#!/usr/bin/env python
from argparse import ArgumentParser
import logging
import json
import os
import sys

from jira import JIRA
from redis import StrictRedis
import six
import yaml
import zendesk

LOG = logging.getLogger(__name__)

ZD_SOLVED_STATUSES = ('solved', 'closed')

def force_yaml_unicode():
    def unicode_str_constructor(loader, node):
        return unicode(loader.construct_scalar(node))

    yaml.add_constructor(u'tag:yaml.org,2002:str', unicode_str_constructor)

if sys.version_info < (3, 0):
    force_yaml_unicode()

class PropertyHolder(object):
    pass

def objectize(dct):
    obj = PropertyHolder()

    for k, v in six.iteritems(dct):
        setattr(obj, k, v)

    return obj

class Bridge(object):
    def __init__(self, jira_client, zd_client, redis, config):
        self.jira_client = jira_client
        self.zd_client = zd_client
        self.redis = redis

        self.jira_url = config.jira_url
        self.jira_issue_jql = config.jira_issue_jql
        self.jira_reference_field = config.jira_reference_field

        self.zd_subject_format = config.zendesk_subject_format
        self.zd_initial_comment_format = config.zendesk_initial_comment_format
        self.zd_comment_format = config.zendesk_comment_format
        self.jira_comment_format = config.jira_comment_format
        self.zd_signature_delimeter = config.zendesk_signature_delimeter

        self.jira_escalation_contact = config.jira_escalation_contact

        self.assignable_groups = list(self.zd_client.assignable_groups)

        self.zd_escalation_group = self._find_group(config.zendesk_escalation_group)
        self.zd_support_group = self._find_group(config.zendesk_support_group)

        self.jira_solved_statuses = config.jira_solved_statuses
        self.jira_priority_map = config.jira_priority_map

        self.zd_initial_fields = self._map_ticket_fields(config.zendesk_initial_fields)

        self.zd_identity = self.zd_client.current_user
        self.jira_identity = self.jira_client.current_user()

    def _find_group(self, name):
        for group in self.assignable_groups:
            if group.name == name:
                return group

        raise ValueError('Could not find group {}'.format(name))

    def _map_ticket_fields(self, mappings):
        result = {}

        for mapping in mappings:
            # TODO Extend later for additional mapping abilities
            result[mapping['id']] = mapping['value']

        return result

    def sync(self):
        for issue in self.jira_client.search_issues(self.jira_issue_jql, fields='assignee,comment,*navigable'):
            try:
                self._sync_issue(issue)
            except:
                LOG.exception('Could not sync issue %s', issue.key)

    def _sync_issue(self, issue):
        update_jira_ref = True

        ticket = self.zd_client.find('type:ticket external_id:{}'.format(issue.key))
        
        if ticket:
            jira_tid_reference = getattr(issue.fields, self.jira_reference_field)

            LOG.debug('Found existing Zendesk ticket %s', ticket.id)

            if jira_tid_reference:
                if jira_tid_reference == str(ticket.id):
                    update_jira_ref = False
                else:
                    raise ValueError('JIRA reference field non-empty and does not match Zendesk ticket ID')
        elif issue.fields.status.name in self.jira_solved_statuses:
            LOG.debug('Skipping previously untracked, solved JIRA issue', issue.key)
            return
        else:
            subject = self.zd_subject_format.format(key=issue.key,
                                                    summary=issue.fields.summary)

            comment_body = self.zd_initial_comment_format.format(creator=issue.fields.creator.displayName,
                                                                 description=issue.fields.description,
                                                                 created=issue.fields.created,
                                                                 key=issue.key,
                                                                 jira_url=self.jira_url)

            if not issue.fields.assignee or issue.fields.assignee.name == self.jira_identity:
                group_id = self.zd_support_group.id
            else:
                group_id = self.zd_escalation_group.id
                self.redis.sadd('escalated_zd_tickets', ticket.id)

            LOG.info('Creating Zendesk ticket for JIRA issue %s', issue.key)
            ticket = self.zd_client.create_ticket(subject=subject,
                                                  comment=dict(body=comment_body),
                                                  external_id=issue.key,
                                                  custom_fields=self.zd_initial_fields,
                                                  group_id=group_id)

        if update_jira_ref:
            self._update_jira_ref(issue, ticket)

        self._take_if_unassigned(issue)

        self._sync_comments_to_jira(issue, ticket)
        self._sync_comments_to_zd(issue, ticket)

        self._sync_priority(issue, ticket)

        self._sync_meta(issue, ticket)

    def _take_if_unassigned(self, issue):
        if not issue.fields.assignee:
            self.jira_client.assign_issue(issue, self.jira_identity)

    def _sync_meta(self, issue, ticket):
        LOG.debug('JIRA issue %s has status %s; Zendesk has %s', issue.key, 
                  issue.fields.status.name, ticket.status)

        if issue.fields.status.name in self.jira_solved_statuses:
            if ticket.status not in ZD_SOLVED_STATUSES:
                LOG.info('Marking Zendesk ticket %s solved', ticket.id)
                ticket.update(status='solved')
        elif ticket.group_id == self.zd_escalation_group:
            LOG.debug('Zendesk ticket %s assigned to myself', ticket.id)
            if self.redis.sismember('escalated_zd_tickets', ticket.id):
                if issue.fields.assignee.name == self.jira_identity:
                    # In the past, the issue was escalated but staff on the JIRA side
                    # have assigned the issue to the bot for de-escalation to Zendesk staff
                    LOG.info('Opening Zendesk ticket %s for de-escalation', issue.key)

                    ticket.update(assignee_id=None, group_id=self.zd_support_group, status='open')
                    self.redis.srem('escalated_zd_tickets', ticket.id)
                else:
                    LOG.debug('JIRA issue %s has not been de-escalated', issue.key)
            else:
                # Zendesk staff assigned the ticket to the escalation group, but it has not been
                # assigned on the JIRA side to the escalation contact
                LOG.info('Assigning JIRA issue %s to escalation contact', issue.key)

                self.jira_client.assign_issue(issue, self.jira_escalation_contact)
                self.redis.sadd('escalated_zd_tickets', ticket.id)
        elif ticket.status != 'open' and (not issue.fields.assignee or issue.fields.assignee.name == self.jira_identity):
                # Retrieve the most recent audit
                last_audit = None
                for audit in ticket.audits:
                    last_audit = audit

                if last_audit.author_id == self.zd_identity.id:
                    # Ticket is not currently escalated and the last update was made by
                    # the bot, so open up the ticket on the Zendesk side
                    LOG.info('Opening Zendesk ticket %s', ticket.id)
                    ticket.update(status='open')

    def _sync_priority(self, issue, ticket):
        priority = self.jira_priority_map[issue.fields.priority.name]
        LOG.debug('JIRA priority %s mapped to Zendesk priority %s', issue.fields.priority.name, priority)

        if ticket.priority != priority:
            LOG.info('Changing Zendesk priority from %s to %s', ticket.priority, priority)
            ticket.update(priority=priority)

    def _update_jira_ref(self, issue, ticket):
        LOG.info('Updating JIRA reference field')
        issue.update(fields={self.jira_reference_field: str(ticket.id)})

    def _sync_comments_to_jira(self, issue, ticket):
        for comment in ticket.comments:
            if not comment.public:
                LOG.debug('Skipping private Zendesk comment %s', comment.id)
                continue

            if comment.author_id == self.zd_identity.id:
                LOG.debug('Skipping my own Zendesk comment %s', comment.id)
                continue

            if self.redis.sismember('seen_zd_comments', comment.id):
                LOG.debug('Skipping seen Zendesk comment %s', comment.id)
                continue

            LOG.info('Copying Zendesk comment %s to JIRA issue', comment.id)

            comment_body = self.jira_comment_format.format(author=comment.author.name,
                                                           created=comment.created_at, 
                                                           body=self._cut_signature(comment.body))

            self.jira_client.add_comment(issue, comment_body)
            self.redis.sadd('seen_zd_comments', comment.id)

    def _sync_comments_to_zd(self, issue, ticket):
        for comment in issue.fields.comment.comments:
            if comment.author.name == self.jira_identity:
                LOG.debug('Skipping my own JIRA comment %s', comment.id)
                continue

            if self.redis.sismember('seen_jira_comments', comment.id):
                LOG.debug('Skipping seen JIRA comment %s', comment.id)
                continue

            LOG.info('Copying JIRA comment %s to Zendesk ticket', comment.id)

            comment_body = self.zd_comment_format.format(author=comment.author.displayName,
                                                         created=comment.created,
                                                         body=comment.body)

            ticket.update(comment=dict(body=comment_body))
            self.redis.sadd('seen_jira_comments', comment.id)

    def _cut_signature(self, body):
        return body.rsplit(self.zd_signature_delimeter, 1)[0]

def configure_logger():
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))

    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG)

def configure_socks_proxy(config):
    import socks
    import sockets

    socks.setdefaultproxy(socks.PROXY_TYPE_SOCKS4, config.socks_proxy_host, config.socks_proxy_port, True)
    socket.socket = socks.socksocket

def main():
    parser = ArgumentParser()
    parser.add_argument('-c', '--config-file', default='config.yaml')

    args = parser.parse_args()

    configure_logger()

    with open(args.config_file) as fp:
        config = objectize(yaml.load(fp))

    if config.socks_proxy:
        configure_socks_proxy(config)

    redis = StrictRedis(host=config.redis_host, port=config.redis_port)

    jira_client = JIRA(server=config.jira_url,
                       basic_auth=(config.jira_username, config.jira_password))

    zd_client = zendesk.Client(url=config.zendesk_url,
                               username=config.zendesk_username,
                               password=config.zendesk_password)

    bridge = Bridge(jira_client=jira_client,
                    zd_client=zd_client,
                    redis=redis,
                    config=config)

    bridge.sync()

if __name__ == '__main__':
    main()
