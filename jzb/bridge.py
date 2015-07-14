import re

import jinja2

from jzb import LOG
from jzb.util import import_class

ACTION_HANDLER_FORMAT = 'handle_{}'

class Bridge(object):
    def __init__(self, jira_client, zd_client, redis, config):
        """
        :param jira_client: `jira.JIRA` object
        :param zd_client: `zendesk.Client` object
        :param redis: `redis.StrictRedis` object
        :param config: object
        """
        self.jira_client = jira_client
        self.zd_client = zd_client
        self.redis = redis

        self.config = config

        self.jira_issue_jql = config.jira_issue_jql
        self.zd_ticket_query_format = jinja2.Template(config.zd_ticket_query_format)

        self.jira_solved_statuses = config.jira_solved_statuses

        self.jira_priority_map = config.jira_priority_map
        self.jira_fallback_priority = config.jira_fallback_priority

        self.jira_reference_field = config.jira_reference_field

        self.zd_subject_format = jinja2.Template(config.zd_subject_format)
        self.zd_initial_comment_format = jinja2.Template(config.zd_initial_comment_format)
        self.zd_followup_comment_format = jinja2.Template(config.zd_followup_comment_format)
        self.zd_comment_format = jinja2.Template(config.zd_comment_format)
        self.jira_comment_format = jinja2.Template(config.jira_comment_format)
        self.zd_signature_delimeter = config.zd_signature_delimeter
        self.jira_url = config.jira_url

        self.zd_identity = zd_client.current_user
        self.jira_identity = jira_client.current_user()

        self.assignable_groups = list(zd_client.assignable_groups)

        self.zd_initial_fields = TicketFieldMapper(list(zd_client.ticket_fields)).map_fields(config.zd_initial_fields)

        self.zd_support_group = self.find_group_by_name(config.zd_support_group)

        self.jira_status_actions = self.parse_status_action_defs(config.jira_status_actions)
        self.zd_status_actions = self.parse_status_action_defs(config.zd_status_actions)

        self.escalation_strategy_defs = self.parse_escalation_strategy_defs(config.escalation_strategies)

        self.ticket_form = self.find_ticket_form_by_name(config.zd_ticket_form)

    def parse_escalation_strategy_defs(self, strategy_defs):
        """
        Parses a list of escalation strategy definitions

        :param strategy_defs: list of dicts
        :return: list of `EscalationStrategyDefinition` objects
        """
        results = []

        for strategy_def in strategy_defs:
            # pop is used so remaining dict entries can be passed directly to escalation strategy
            strategy_class = import_class(strategy_def.pop('type'))
            group = strategy_def.pop('group')

            results.append(EscalationStrategyDefinition(
                group_pattern=re.compile(group),
                strategy=strategy_class(**strategy_def),
            ))

        return results

    def parse_status_action_defs(self, action_defs):
        """
        Parses a list of status action definitions

        :param action_defs: list of dicts
        :return: list of `ActionDefinition` objects
        """
        results = []

        for action_def in action_defs:
            actions = []

            for action in action_def['actions']:
                # pop is used so remaining dict entries can be passed directly to action
                handler_name = ACTION_HANDLER_FORMAT.format(action.pop('type'))
                description = action.pop('description')
                only_once = action.pop('only_once', False)

                actions.append(Action(
                    handler=getattr(self, handler_name),
                    description=description,
                    params=action,
                    only_once=only_once,
                ))

            results.append(ActionDefinition(
                jira_status=action_def['jira_status'],
                zd_status=action_def['zd_status'],
                actions=actions,
                description=action_def['description'],
                force=action_def.get('force', False),
            ))

        return results

    def sync(self):
        """
        Attempts to sync issues matching the configures JQL query
        """
        LOG.debug('Querying JIRA: %s', self.jira_issue_jql)
        for issue in self.jira_client.search_issues(self.jira_issue_jql, 
                                                    fields='assignee,attachment,comment,*navigable'):
            try:
                LOG.debug('Syncing JIRA issue: %s', issue.key)
                self.sync_issue(SyncContext(issue))
            except:
                LOG.exception('Failed to sync issue: %s', issue.key)

    def sync_issue(self, ctx):
        """
        Syncs a given issue with one or more tickets in Zendesk

        :param ctx: `SyncContext` object
        """
        if not self.ensure_ticket_if_eligible(ctx):
            return

        self.sync_jira_reference(ctx)
        self.sync_priority(ctx)
        self.sync_assignee(ctx)
        self.sync_zd_comments_to_jira(ctx)
        self.sync_jira_comments_to_zd(ctx)
        self.sync_status(ctx)

    def ensure_ticket_if_eligible(self, ctx):
        """
        Retrieves or creates a ticket in Zendesk if the given JIRA issue is eligible for bridging. 
        If the issue is previously untracked and meets any of the following conditions, it will 
        not be eligible for bridging:

        * The status is present in the jira_solved_statuses list
        * The assignee is set, but is not the bridge user

        If the ticket was closed but the eligible issue is not, then a followup ticket will be created.

        :param ctx: `SyncContext` object
        """
        issue = ctx.issue

        ticket_id = self.redis.get('zd_ticket:{}'.format(issue.key))
        if ticket_id:
            ticket = self.zd_client.ticket(ticket_id)
        else:
            ticket = self.zd_client.find_first(self.zd_ticket_query_format.render(issue=issue),
                                               sort_by='created_at',
                                               sort_order='desc')

        if not ticket:
            if not self.is_issue_eligible(issue):
                LOG.debug('Skipping previously untracked, ineligible issue')
                return False

            LOG.info('Creating Zendesk ticket for JIRA issue')
            ticket = self.create_ticket(issue)
        elif ticket.status == 'closed':
            if not self.is_issue_eligible(issue):
                LOG.debug('Skipping previously closed, ineligible issue')
                return False

            LOG.info('Creating followup Zendesk ticket for JIRA issue')
            ticket = self.create_followup_ticket(issue, ticket)

        ctx.ticket = ticket

        # Cache ticket mapping locally, Zendesk search is strictly rate limited
        self.redis.set('zd_ticket:{}'.format(issue.key), ticket.id)

        return True

    def is_issue_eligible(self, issue):
        """
        Determines if an untracked or previously closed issue is eligible for creation in Zendesk

        :param issue: `jira.resources.Issue` object
        :return: Whether or not issue is eligible
        """
        if issue.fields.status.name in self.jira_solved_statuses:
            # Ignore issues that have already been marked solved
            return False

        if issue.fields.assignee and issue.fields.assignee.name != self.jira_identity:
            # Ignore issues that have already been assigned to someone other than us
            return False

        return True

    def create_ticket(self, issue):
        """
        Creates a ticket corresponding to an eligible JIRA issue

        :param issue: `jira.resources.Issue` object
        :return: `zendesk.resources.Ticket` object
        """
        subject = self.zd_subject_format.render(issue=issue)

        comment = self.zd_initial_comment_format.render(issue=issue,
                                                        jira_url=self.jira_url)

        return self.zd_client.create_ticket(subject=subject,
                                            comment=dict(body=comment),
                                            external_id=issue.key,
                                            custom_fields=self.zd_initial_fields,
                                            group_id=self.zd_support_group.id,
                                            ticket_form_id=self.ticket_form.id)

    def create_followup_ticket(self, issue, previous_ticket):
        """
        Creates a followup ticket corresponding to an eligible JIRA issue

        :param issue: `jira.resources.Issue` object
        :param previous_ticket: `zendesk.resources.Ticket` object representing the ticket to followup on
        :return: `zendesk.resources.Ticket` object
        """
        subject = self.zd_subject_format.render(issue=issue)

        comment = self.zd_followup_comment_format.render(issue=issue,
                                                         jira_url=self.jira_url)

        return self.zd_client.create_ticket(subject=subject,
                                            comment=dict(body=comment),
                                            external_id=issue.key,
                                            custom_fields=self.zd_initial_fields,
                                            group_id=self.zd_support_group.id,
                                            ticket_form_id=self.ticket_form.id,
                                            via_followup_source_id=previous_ticket.id)

    def sync_assignee(self, ctx):
        """
        Transitions the assignee/group when changes are detected

        :param ctx: `SyncContext` object
        """
        last_seen_jira_assignee = self.redis.get('last_seen_jira_assignee:{}'.format(ctx.issue.key))
        last_seen_zd_group = self.redis.get('last_seen_zd_group:{}'.format(ctx.ticket.id))

        if not ctx.issue.fields.assignee:
            LOG.info('Assigning previously unassigned JIRA issue to bot')
            self.jira_client.assign_issue(ctx.issue, self.jira_identity)
            self.refresh_issue(ctx)
        elif ctx.issue.fields.assignee.name != last_seen_jira_assignee:
            if (ctx.issue.fields.assignee.name == self.jira_identity and 
                    ctx.ticket.group_id != self.zd_support_group.id):
                LOG.info('Assigning Zendesk ticket to group: %s', self.zd_support_group.name)
                ctx.ticket = ctx.ticket.update(group_id=self.zd_support_group.id)
        elif str(ctx.ticket.group_id) != last_seen_zd_group:
            if ctx.ticket.group_id != self.zd_support_group.id:
                self.handle_escalation(ctx)
        else:
            return
        
        self.redis.set('last_seen_jira_assignee:{}'.format(ctx.issue.key), ctx.issue.fields.assignee.name)
        self.redis.set('last_seen_zd_group:{}'.format(ctx.ticket.id), ctx.ticket.group_id)

    def handle_escalation(self, ctx):
        """
        Handles when Zendesk agents escalate a ticket to another group

        :param ctx: `SyncContext` object
        """
        group = self.find_group_by_id(ctx.ticket.group_id)

        LOG.debug('Zendesk ticket assigned to group: %s', group.name)

        match = False
        for strategy_def in self.escalation_strategy_defs:
            if strategy_def.group_pattern.match(group.name):
                assignee = strategy_def.strategy.get_escalation_contact()

                LOG.info('Assigning JIRA issue to user: %s', assignee)
                self.jira_client.assign_issue(ctx.issue, assignee)
                self.refresh_issue(ctx)

                try:
                    strategy_def.strategy.post_escalation()
                except:
                    LOG.exception('Failed to call post-escalation hook on strategy')
                
                match = True
                break

        if not match:
            LOG.warn('Could not match group to escalation strategy')

    def sync_status(self, ctx):
        """
        Transitions the status on both sides when out of sync, with preference
        for the JIRA status

        :param ctx: `SyncContext` object
        """
        last_seen_jira_status = self.redis.get('last_seen_jira_status:{}'.format(ctx.issue.key))
        last_seen_zd_status = self.redis.get('last_seen_zd_status:{}'.format(ctx.ticket.id))

        LOG.debug('JIRA status: %s; Zendesk status: %s', ctx.issue.fields.status.name, ctx.ticket.status)

        owned = ctx.issue.fields.assignee.name == self.jira_identity
        if not owned:
            LOG.debug('Issue not owned by us, action defs without "force" will not apply')

        jira_status_changed = last_seen_jira_status != ctx.issue.fields.status.name
        if jira_status_changed:
            LOG.debug('JIRA status changed')

        zd_status_changed = last_seen_zd_status != ctx.ticket.status
        if zd_status_changed:
            LOG.debug('Zendesk status changed')

        self.process_status_actions(ctx, self.jira_status_actions, jira_status_changed, owned)
        self.process_status_actions(ctx, self.zd_status_actions, zd_status_changed, owned)

        self.redis.set('last_seen_jira_status:{}'.format(ctx.issue.key), ctx.issue.fields.status.name)
        self.redis.set('last_seen_zd_status:{}'.format(ctx.ticket.id), ctx.ticket.status)

    def process_status_actions(self, ctx, action_defs, changed, owned):
        """
        Runs matching status action definitions against an issue/ticket pair

        :param ctx: `SyncContext` object
        :param action_defs: list of `ActionDefinition` objects
        :param changed: True if status has changed
        :param owned: True if issue is owned by bot
        """
        while True:
            match = False

            for action_def in action_defs:
                if not owned and not action_def.force:
                    continue

                if not (ctx.issue.fields.status.name in action_def.jira_status and
                            ctx.ticket.status in action_def.zd_status):
                    continue

                LOG.debug('Matched action def: %s', action_def.description)
                match = True

                for action in action_def.actions:
                    if action.only_once and not changed:
                        LOG.debug('Skipping action marked only_once: %s', action.description)
                        continue

                    try:
                        LOG.info('Performing action: %s', action.description)
                        action.handle(ctx)
                    except:
                        LOG.exception('Failed to perform action')
                        return

                break
            
            if not match:
                LOG.debug('No action defs matched')
                break

    def sync_jira_reference(self, ctx):
        """
        Syncs a specified custom field on a JIRA issue with the ID of the Zendesk ticket

        :param ctx: `SyncContext` object
        """
        if not self.jira_reference_field:
            return

        ticket_id = str(ctx.ticket.id)
        if getattr(ctx.issue.fields, self.jira_reference_field) != ticket_id:
            LOG.info('Updating JIRA reference for ticket: %s', ticket_id)
            ctx.issue.update(fields={self.jira_reference_field: ticket_id})

    def sync_priority(self, ctx):
        """
        Syncs the Zendesk ticket priority from the JIRA issue priority

        :param ctx: `SyncContext` object
        """
        jira_priority = ctx.issue.fields.priority.name
        zd_priority = self.jira_priority_map.get(jira_priority, self.jira_fallback_priority)

        LOG.debug('JIRA priority: %s; mapped Zendesk priority: %s', jira_priority, zd_priority)
        
        if ctx.ticket.priority != zd_priority:
            LOG.info('Updating Zendesk ticket priority')
            ctx.ticket.update(priority=zd_priority)
            self.refresh_ticket(ctx)

    def sync_zd_comments_to_jira(self, ctx):
        """
        Synchronizes comments on the Zendesk ticket to the JIRA issue

        :param ctx: `SyncContext` object
        """
        changed = False

        for comment in ctx.ticket.comments:
            if not comment.public:
                LOG.debug('Skipping private Zendesk comment %s', comment.id)
                continue

            if comment.author_id == self.zd_identity.id:
                LOG.debug('Skipping my own Zendesk comment: %s', comment.id)
                continue

            if self.redis.sismember('seen_zd_comments', comment.id):
                LOG.debug('Skipping seen Zendesk comment: %s', comment.id)
                continue

            LOG.info('Copying Zendesk comment to JIRA issue: %s', comment.id)

            stripped_body = comment.body.rsplit(self.zd_signature_delimeter, 1)[0]

            comment_body = self.jira_comment_format.render(comment=comment,
                                                           stripped_body=stripped_body)

            self.jira_client.add_comment(ctx.issue, comment_body)
            self.redis.sadd('seen_zd_comments', comment.id)
            
            changed = True

        if changed:
            self.refresh_issue(ctx)

    def sync_jira_comments_to_zd(self, ctx):
        """
        Synchronizes comments on the JIRA issue to the Zendesk ticket

        :param ctx: `SyncContext` object
        """
        for comment in ctx.issue.fields.comment.comments:
            if comment.author.name == self.jira_identity:
                LOG.debug('Skipping my own JIRA comment: %s', comment.id)
                continue

            if self.redis.sismember('seen_jira_comments', comment.id):
                LOG.debug('Skipping seen JIRA comment: %s', comment.id)
                continue

            LOG.info('Copying JIRA comment to Zendesk ticket: %s', comment.id)

            comment_body = self.zd_comment_format.render(comment=comment)

            ctx.ticket.update(comment=dict(body=comment_body))
            self.redis.sadd('seen_jira_comments', comment.id)

    def find_group_by_name(self, name):
        """
        Find group object by its name

        :param name: Name of the group to find
        :return: `zendesk.resources.Group` object
        """
        for group in self.assignable_groups:
            if group.name == name:
                return group

        raise ValueError('Could not find group by name: {}'.format(name))

    def find_group_by_id(self, id):
        """
        Find group object by its id

        :param id: Integer id
        :return: `zendesk.resources.Group` object
        """
        for group in self.assignable_groups:
            if group.id == id:
                return group

        raise ValueError('Could not find group by id: {}'.format(id))

    def find_ticket_form_by_name(self, name):
        """
        Find ticket form by its name

        :param name: Name of the ticket form to find
        :return: `zendesk.resources.TicketForm` object
        """
        for form in self.zd_client.ticket_forms:
            if form.name == name:
                return form

        raise ValueError('Could not find ticket form by name: {}'.format(name))

    def refresh_ticket(self, ctx):
        """
        Refresh ticket from the Zendesk API

        :param ctx: `SyncContext` object
        """
        if ctx.ticket:
            ctx.ticket = self.zd_client.ticket(ctx.ticket.id)

    def refresh_issue(self, ctx):
        """
        Refresh issue from the JIRA API

        :param ctx: `SyncContext` object
        """
        ctx.issue = self.jira_client.issue(ctx.issue.key, fields='assignee,attachment,comment,*navigable')

    def handle_update_ticket(self, ctx, **kwargs):
        """
        Handler for the `update_ticket` action type

        :param ctx: `SyncContext` object
        """
        ctx.ticket = ctx.ticket.update(**kwargs)

    def handle_transition_issue(self, ctx, name, **kwargs):
        """
        Handler for the `transition_issue` action type

        :param ctx: `SyncContext` object
        :param name: name of the transition to perform
        """
        params = kwargs.copy()

        transitions = self.jira_client.transitions(ctx.issue)
        transition_id = None
        for transition in transitions:
            if transition['name'] == name:
                transition_id = transition['id']
                break

        if not transition_id:
            raise ValueError('Could not find transition: %s', name)

        self.jira_client.transition_issue(ctx.issue, transition_id, fields=params)
        self.refresh_issue(ctx)

    def handle_add_ticket_tags(self, ctx, tags, **kwargs):
        """
        Handler for the `add_ticket_tags` action type

        :param ctx: `SyncContext` object
        :param tags: list of tag names to add
        """
        absent_tags = []
        for tag in tags:
            if tag not in ctx.ticket.tags:
                absent_tags.append(tag)

        if absent_tags:
            ctx.ticket.add_tags(*absent_tags)
            self.refresh_ticket(ctx)

    def handle_remove_ticket_tags(self, ctx, tags, **kwargs):
        """
        Handler for the `remove_ticket_tags` action type

        :param ctx: `SyncContext` object
        :param tags: list of tag names to remove
        """
        present_tags = []
        for tag in tags:
            if tag in ctx.ticket.tags:
                present_tags.append(tag)

        if present_tags:
            ctx.ticket.remove_tags(*present_tags)
            self.refresh_ticket(ctx)
    
class SyncContext(object):
    """
    Container for an issue/ticket pair
    """
    def __init__(self, issue):
        """
        :param issue: `jira.resources.Issue` object
        """
        self.issue = issue
        self.ticket = None

class TicketFieldMapper(object):
    """
    Maps a number of field mappings into a format acceptable for Zendesk's custom_fields parameter

    The following list of mappings are an example.

    ```yaml
    - id: 111
      value: XXX

    - name: Product
      value: YYY
    ```

    This mapper will attempt to turn it into the following:

    ```yaml
    111: XXX
    222: YYY
    ```
    """
    def __init__(self, ticket_fields):
        """
        :param issue: `jira.resources.Issue` object
        """
        self.ticket_fields = ticket_fields

    def map_fields(self, mappings):
        """
        :param mappings: list of dictionary mappings
        :return: dictionary that can be consumed by the Zendesk API
        """
        result = {}

        for mapping in mappings:
            id = self.map_field_id(mapping)
            result[id] = mapping['value']

        return result

    def map_field_id(self, mapping):
        """
        :param mapping: dictionary mapping
        :return: int
        """
        if 'id' in mapping:
            return mapping['id']
        elif 'name' in mapping:
            return self.find_active_ticket_field(mapping['name']).id
        else:
            raise ValueError('Could not map ticket field ID from: {}'.format(mapping))

    def map_field_value(self, mapping):
        """
        :param mapping: dictionary mapping
        :return: object
        """
        if 'value' in mapping:
            return mapping['value']
        else:
            raise ValueError('Could not map ticket field value from: {}'.format(mapping))
        
    def find_active_ticket_field(self, name):
        """
        :param name: Name of field to find
        :return: `zendesk.resources.TicketField` object
        """
        for field in self.ticket_fields:
            if field.name == name and field.active:
                return field

        raise ValueError('Could not find active ticket field by name: {}'.format(name))

class ActionDefinition(object):
    def __init__(self, jira_status, zd_status, actions, description, force):
        self.jira_status = jira_status
        self.zd_status = zd_status
        self.actions = actions
        self.description = description
        self.force = force

class Action(object):
    def __init__(self, handler, params, description, only_once):
        self.handler = handler
        self.params = params
        self.description = description
        self.only_once = only_once

    def handle(self, ctx):
        if self.params:
            return self.handler(ctx, **self.params)
        else:
            return self.handler(ctx)

class EscalationStrategyDefinition(object):
    def __init__(self, group_pattern, strategy):
        self.group_pattern = group_pattern
        self.strategy = strategy
