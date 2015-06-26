# jira-zd-bridge

## Production

```
git clone https://github.com/ianunruh/jira-zd-bridge.git -b next
cd jira-zd-bridge

pip install -U -r requirements.txt
python setup.py install

jzb --help
```

## Development

Install and start Redis in one terminal

```bash
brew install redis
redis-server
```

Install and run local JIRA instance

```bash
brew tap atlassian/tap
brew install atlassian-plugin-sdk
atlas-run-standalone --product jira
```

Configure acceptance tests

```bash
cp config.sample.yml config-test.yml
cp test_cases.sample.yml test_cases.yml

vim config-test.yml
```

Install tox and run acceptance tests

```bash
# If using pyenv
pyenv global 2.7.9 3.4.3

pip install tox

tox
```
