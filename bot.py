#!/usr/bin/env python
from argparse import ArgumentParser
import logging
import json
import os
import sys

from jira import JIRA
from redis import StrictRedis
import requests
import yaml

LOG = logging.getLogger(__name__)

def force_yaml_unicode():
    def unicode_str_constructor(loader, node):
        return unicode(loader.construct_scalar(node))

    yaml.add_constructor(u'tag:yaml.org,2002:str', unicode_str_constructor)

force_yaml_unicode()

class MultipleResults(Exception):
    pass

def raise_for_status(func):
    def decorator(*args, **kwargs):
        response = func(*args, **kwargs)

        try:
            response.raise_for_status()
        except:
            print response.text
            raise

        return response

    return decorator

class PropertyHolder(object):
    pass

def json2obj(raw):
    if isinstance(raw, list):
        return [json2obj(x) for x in raw]
    elif isinstance(raw, dict):
        obj = PropertyHolder()
        for k, v in raw.items():
            setattr(obj, k, v)
        return obj

    return raw

def map_json(func):
    def decorator(*args, **kwargs):
        return json2obj(func(*args, **kwargs))

    return decorator

class ZendeskClient(object):
    def __init__(self, url, username, password):
        self.url = url
        self.username = username
        self.password = password

    @property
    @map_json
    def me(self):
        return self.get('/api/v2/users/me.json').json()['user']

    @map_json
    def search(self, query, sort_by=None, sort_order=None):
        params = {
            'query': query
        }

        if sort_by:
            params['sort_by'] = sort_by

        if sort_order:
            params['sort_order'] = sort_order

        params['page'] = 1

        results = []
        while True:
            body = self.get('/api/v2/search.json', params=params).json()

            results.extend(body['results'])

            if not body['next_page']:
                return results

            params['page'] += 1

    @map_json
    def find(self, query):
        results = self.search(query)

        if len(results) > 1:
            raise MultipleResults()
        elif len(results) == 1:
            return results[0]

    @map_json
    def ticket(self, id):
        return self.get('/api/v2/tickets/{}.json'.format(id)).json()['ticket']

    @map_json
    def create_ticket(self, subject, comment_body, external_id=None):
        params = {
            'ticket': {
                'subject': subject,
                'comment': {
                    'body': comment_body,
                },
                'external_id': external_id,
            }
        }

        return self.post('/api/v2/tickets.json', json=params).json()['ticket']

    @map_json
    def update_ticket(self, id, **kwargs):
        params = {
            'ticket': kwargs,
        }

        return self.put('/api/v2/tickets/{}.json'.format(id), json=params).json()['ticket']

    @map_json
    def ticket_comments(self, id):
        return self.get('/api/v2/tickets/{}/comments.json'.format(id)).json()['comments']

    @map_json
    def create_ticket_comment(self, id, body, public=False, status='open'):
        params = {
            'body': body,
            'public': public,
        }

        return self.update_ticket(id, comment=params, status=status)

    @map_json
    def user(self, id):
        return self.get('/api/v2/users/{}.json'.format(id)).json()['user']

    @raise_for_status
    def get(self, url, **kwargs):
        return requests.get(self.url + url, auth=(self.username, self.password), **kwargs)

    @raise_for_status
    def post(self, url, **kwargs):
        return requests.post(self.url + url, auth=(self.username, self.password), **kwargs)

    @raise_for_status
    def put(self, url, **kwargs):
        return requests.put(self.url + url, auth=(self.username, self.password), **kwargs)

class Bridge(object):
    def __init__(self, jira_client, zd_client, redis, config):
        self.jira_client = jira_client
        self.zd_client = zd_client
        self.redis = redis

        self.jira_issue_jql = config['jira_issue_jql']
        self.jira_reference_field = config['jira_reference_field']

        self.zd_subject_format = config['zendesk_subject_format']
        self.zd_initial_comment_format = config['zendesk_initial_comment_format']
        self.zd_comment_format = config['zendesk_comment_format']
        self.jira_comment_format = config['jira_comment_format']
        self.zd_signature_delimeter = config['zendesk_signature_delimeter']

        self.zd_identity = self.zd_client.me
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
            # NOTE(ianunruh) The PropertyHolder object does not expose `get` or `__getitem__`
            jira_tid_reference = issue.fields.__dict__[self.jira_reference_field]

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
                                                  comment_body=comment_body,
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

            author = self.zd_client.user(comment.author_id)

            comment_body = self.jira_comment_format.format(author=author.name,
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

            self.zd_client.create_ticket_comment(ticket.id, comment_body, public=True)
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
        config = yaml.load(fp)

    redis = StrictRedis(host=config['redis_host'], port=config['redis_port'])

    jira_client = JIRA(server=config['jira_url'],
                       basic_auth=(config['jira_username'], config['jira_password']))

    zd_client = ZendeskClient(url=config['zendesk_url'],
                              username=config['zendesk_username'],
                              password=config['zendesk_password'])

    bridge = Bridge(jira_client=jira_client,
                    zd_client=zd_client,
                    redis=redis,
                    config=config)

    bridge.sync()

if __name__ == '__main__':
    main()
