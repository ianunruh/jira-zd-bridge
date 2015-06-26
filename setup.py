#!/usr/bin/env python
from setuptools import setup, find_packages

setup(
    name='jzb',
    version='0.0.1',
    author='Ian Unruh',
    author_email='ianunruh@gmail.com',
    url='https://github.com/ianunruh/jira-zd-bridge',
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'jzb = jzb.runner:main',
        ],
    },
)
