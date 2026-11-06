"""
Copyright (C) 2026 Lightwell

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

         http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from unittest.mock import MagicMock, patch

from taisce_cuan.cli import create_parser, handle_push_git_source


def test_push_git_source_parses_required_args():
    parser = create_parser()
    args = parser.parse_args([
        "push-git-source",
        "--upstream-url", "https://github.com/mkdocstrings/griffe.git",
        "--ref", "2.3.2",
        "--package", "griffe",
        "--version", "2.3.2",
        "--forge-url", "https://forge.example.com",
        "--group", "testgroup",
        "--committer-name", "bot",
        "--committer-email", "bot@example.com",
    ])
    assert args.command == "push-git-source"
    assert args.upstream_url == "https://github.com/mkdocstrings/griffe.git"
    assert args.ref == "2.3.2"


@patch("taisce_cuan.cli.GitMirrorPublisher")
def test_handle_push_git_source_calls_publisher(mock_publisher_cls):
    parser = create_parser()
    args = parser.parse_args([
        "push-git-source",
        "--upstream-url", "https://github.com/mkdocstrings/griffe.git",
        "--ref", "2.3.2",
        "--package", "griffe",
        "--version", "2.3.2",
        "--workspace-dir", "/tmp/work",
        "--forge-url", "https://forge.example.com",
        "--group", "testgroup",
        "--committer-name", "bot",
        "--committer-email", "bot@example.com",
    ])
    mock_publisher = mock_publisher_cls.return_value
    mock_publisher.publish_git_source.return_value = MagicMock(tag_name="griffe/2.3.2")

    exit_code = handle_push_git_source(args)

    assert exit_code == 0
    request = mock_publisher.publish_git_source.call_args[0][0]
    assert request.upstream_url == "https://github.com/mkdocstrings/griffe.git"
    assert request.ref == "2.3.2"
    assert request.package == "griffe"
    assert request.version == "2.3.2"
