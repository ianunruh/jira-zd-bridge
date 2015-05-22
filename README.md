# jira-zd-bridge

## Development

Install and start Redis in one terminal

```bash
brew install redis
redis-server
```

Install dependencies and configure

```bash
virtualenv env
source env/bin/activate

pip install -r requirements.txt

cp config.sample.yaml config.yaml

python bot.py
```

Install and run local JIRA instance

```bash
brew tap atlassian/tap
brew install atlassian-plugin-sdk
atlas-run-standalone --product jira
```
