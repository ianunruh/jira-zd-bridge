import code
import logging
import os
import unittest

import jira
from redis import StrictRedis
import six
import yaml
import zendesk

from jzb import LOG
from jzb.bridge import Bridge, SyncContext
from jzb.runner import configure_logger
from jzb.util import objectize

configure_logger(logging.DEBUG)

class IntegrationTest(unittest.TestCase):
    def setUp(self):
        with open('config-test.yml') as fp:
            config = objectize(yaml.load(fp))

        self.redis = StrictRedis(host=config.redis_host, port=config.redis_port)

        self.jira_client = jira.JIRA(server=config.jira_url,
                                     basic_auth=(config.jira_username, config.jira_password))

        self.zd_client = zendesk.Client(url=config.zd_url,
                                        username=config.zd_username,
                                        password=config.zd_password)

        self.config = config
        
        self.bridge = Bridge(jira_client=self.jira_client,
                                zd_client=self.zd_client,
                                redis=self.redis,
                                config=self.config)

        self.jira_identities = {}
        for name, creds in six.iteritems(config.jira_identities):
            self.jira_identities[name] = jira.JIRA(server=config.jira_url,
                                                   basic_auth=(creds['username'], creds['password']))
        
        self.zd_identities = {}
        for name, creds in six.iteritems(config.zd_identities):
            self.zd_identities[name] = zendesk.Client(url=config.zd_url, **creds)

    def tearDown(self):
        pass

    @unittest.skipUnless(os.path.isfile('test_cases.yml'), 'test_cases.yml not present')
    def test_all_the_things(self):
        with open('test_cases.yml') as fp:
            cases = yaml.load(fp)

        one_test = os.environ.get('JZB_TEST_CASE')

        if one_test:
            self._run_test_case(one_test, cases[one_test])
        else:
            for name, case in six.iteritems(cases):
                self._run_test_case(name, case)

    def _run_test_case(self, name, case):
        LOG.info('=== Performing acceptance test case: %s', name)

        tickets = {}

        try:
            issue = self.jira_client.create_issue(fields=case['issue'])

            ctx = SyncContext(issue)
            
            for step in case['steps']:
                for precondition in step.get('preconditions', {}):
                    try:
                        assert eval(precondition, globals(), dict(issue=ctx.issue, ticket=ctx.ticket))
                    except AssertionError:
                        LOG.error('Precondition failed: %s', precondition)
                        raise

                for action in step['actions']:
                    handler = 'handle_{}'.format(action.pop('type'))
                    getattr(self, handler)(ctx, **action)

                self.bridge.sync_issue(ctx)

                for assertion in step.get('assertions', {}):
                    try:
                        assert eval(assertion, globals(), dict(issue=ctx.issue, ticket=ctx.ticket))
                    except AssertionError:
                        LOG.error('Assertion failed: %s', assertion)
                        raise

                if ctx.ticket and ctx.ticket.id not in tickets:
                    tickets[ctx.ticket.id] = ctx.ticket
        finally:
            cleanup_issue(ctx.issue)
            [cleanup_ticket(x) for x in six.itervalues(tickets)]

    def handle_update_ticket(self, ctx, **kwargs):
        self.bridge.handle_update_ticket(ctx, **kwargs)

    def handle_assign_issue(self, ctx, assignee):
        self.jira_client.assign_issue(ctx.issue, assignee)
        self.bridge.refresh_issue(ctx)

    def handle_transition_issue(self, ctx, name, **kwargs):
        self.bridge.handle_transition_issue(ctx, name, **kwargs)

    def handle_add_issue_comment(self, ctx, body, as_identity=None):
        if as_identity:
            jira_client = self.jira_identities[as_identity]
        else:
            jira_client = self.jira_client

        jira_client.add_comment(ctx.issue, body)
        self.bridge.refresh_issue(ctx)

    def handle_add_ticket_comment(self, ctx, as_identity=None, **kwargs):
        if as_identity:
            zd_client = self.zd_identities[as_identity]
        else:
            zd_client = self.zd_client

        zd_client.update_ticket(ctx.ticket.id, comment=dict(**kwargs))

    def handle_repl(self, ctx, **kwargs):
        code.interact(local=locals())

def cleanup_issue(issue):
    try:
        LOG.debug('Trying to delete issue: %s', issue.key)
        issue.delete()
    except:
        LOG.exception('Failed to delete issue')

def cleanup_ticket(ticket):
    try:
        LOG.debug('Trying to delete ticket: %s', ticket.id)
        ticket.delete()
    except:
        LOG.exception('Failed to delete ticket')
