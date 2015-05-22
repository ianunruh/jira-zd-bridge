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

        self.jira_issue_jql = config.jira_issue_jql
        self.jira_reference_field = config.jira_reference_field

        self.zd_subject_format = config.zendesk_subject_format
        self.zd_initial_comment_format = config.zendesk_initial_comment_format
        self.zd_comment_format = config.zendesk_comment_format
        self.jira_comment_format = config.jira_comment_format
        self.zd_signature_delimeter = config.zendesk_signature_delimeter

        self.zd_identity = self.zd_client.current_user
        self.jira_identity = self.jira_client.current_user()

    def sync(self):
        for issue in self.jira_client.search_issues(self.jira_issue_jql, fields='comment,*navigable', expand='changelog'):
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
        else:
            subject = self.zd_subject_format.format(key=issue.key,
                                                    summary=issue.fields.summary)

            comment_body = self.zd_initial_comment_format.format(creator=issue.fields.creator.displayName,
                                                                 description=issue.fields.description,
                                                                 created=issue.fields.created,
                                                                 key=issue.key)

            LOG.debug('Creating Zendesk ticket')
            ticket = self.zd_client.create_ticket(subject=subject,
                                                  comment=dict(body=comment_body),
                                                  external_id=issue.key)

            LOG.info('Created Zendesk ticket %s', ticket.id)

        if update_jira_ref:
            self._update_jira_ref(issue, ticket)

        self._sync_zendesk_comments(issue, ticket)
        self._sync_jira_comments(issue, ticket)

    def _update_jira_ref(self, issue, ticket):
            LOG.debug('Updating JIRA reference field')
            issue.update(fields={self.jira_reference_field: str(ticket.id)})

    def _sync_zendesk_comments(self, issue, ticket):
        for comment in self.zd_client.ticket_comments(ticket.id):
            if not comment.public:
                LOG.debug('Skipping private Zendesk comment %s', comment.id)
                continue

            if comment.author_id == self.zd_identity.id:
                LOG.debug('Skipping my own Zendesk comment %s', comment.id)
                continue

            if self.redis.sismember('seen_zd_comments', comment.id):
                LOG.debug('Skipping seen Zendesk comment %s', comment.id)
                continue

            LOG.debug('Copying Zendesk comment %s to JIRA issue', comment.id)

            comment_body = self.jira_comment_format.format(author=comment.author.name,
                                                           created=comment.created_at, 
                                                           body=self._cut_signature(comment.body))

            self.jira_client.add_comment(issue, comment_body)
            self.redis.sadd('seen_zd_comments', comment.id)

            LOG.info('Copied Zendesk comment %s to JIRA ticket', comment.id)

    def _sync_jira_comments(self, issue, ticket):
        for comment in issue.fields.comment.comments:
            if comment.author.name == self.jira_identity:
                LOG.debug('Skipping my own JIRA comment %s', comment.id)
                continue

            if self.redis.sismember('seen_jira_comments', comment.id):
                LOG.debug('Skipping seen JIRA comment %s', comment.id)
                continue

            LOG.debug('Copying JIRA comment %s to Zendesk ticket', comment.id)

            comment_body = self.zd_comment_format.format(author=comment.author.displayName,
                                                         created=comment.created,
                                                         body=comment.body)

            self.zd_client.update_ticket(ticket.id, comment=dict(body=comment_body), status='open')
            self.redis.sadd('seen_jira_comments', comment.id)

            LOG.info('Copied JIRA comment %s to Zendesk ticket', comment.id)

    def _cut_signature(self, body):
        return body.rsplit(self.zd_signature_delimeter, 1)[0]

def configure_logger():
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))

    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG)

def main():
    parser = ArgumentParser()
    parser.add_argument('-c', '--config-file', default='config.yaml')

    args = parser.parse_args()

    configure_logger()

    with open(args.config_file) as fp:
        config = objectize(yaml.load(fp))

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
