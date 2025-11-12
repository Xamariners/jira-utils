from __future__ import annotations

from pathlib import Path

from setuptools import find_packages, setup

README = Path(__file__).with_name("README.md").read_text()

setup(
    name="jira-utils-cli",
    version="0.1.0",
    description="Utilities for inventorying and cleaning Jira attachments.",
    long_description=README,
    long_description_content_type="text/markdown",
    author="ben",
    python_requires=">=3.9",
    packages=find_packages(include=("jira_utils", "jira_utils.*")),
    install_requires=["requests>=2.31"],
    entry_points={"console_scripts": ["jira-utils=jira_utils.cli:main"]},
)
