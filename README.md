# jira-zd-bridge

## Development

Install and start Redis in one terminal

```
brew install redis
redis-server
```

Install dependencies and configure

```
virtualenv env
source env/bin/activate

pip install -r requirements.txt

cp config.sample.yaml config.yaml

python bot.py
```
