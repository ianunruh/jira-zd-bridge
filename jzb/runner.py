from argparse import ArgumentParser
import logging
import sys

import jira
from redis import StrictRedis
import yaml
import zendesk

from jzb import LOG
from jzb.bridge import Bridge
from jzb.util import objectize

def configure_logger(level):
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))

    LOG.addHandler(handler)
    LOG.setLevel(level)

def main():
    parser = ArgumentParser()
    parser.add_argument('-c', '--config-file', default='config.yml')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('-Q', '--query')

    args = parser.parse_args()

    if args.verbose:
        configure_logger(logging.DEBUG)
    else:
        configure_logger(logging.INFO)

    with open(args.config_file) as fp:
        config = objectize(yaml.load(fp))

    redis = StrictRedis(host=config.redis_host, port=config.redis_port)

    jira_client = jira.JIRA(server=config.jira_url,
                            basic_auth=(config.jira_username, config.jira_password))

    zd_client = zendesk.Client(url=config.zd_url,
                               username=config.zd_username,
                               password=config.zd_password)

    bridge = Bridge(jira_client=jira_client,
                    zd_client=zd_client,
                    redis=redis,
                    config=config)

    if args.query:
        bridge.jira_issue_jql = args.query

    bridge.sync()

if __name__ == '__main__':
    main()
