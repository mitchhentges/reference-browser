# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""
Decision task for pull requests and pushes
"""

from __future__ import print_function

import argparse
import datetime
import json
import os
import taskcluster
import sys

import lib.build_variants
from lib.taskgraph import TaskGraph
import lib.tasks

TASK_ID = os.environ.get('TASK_ID')
REPO_URL = os.environ.get('MOBILE_HEAD_REPOSITORY')
BRANCH = os.environ.get('MOBILE_HEAD_BRANCH')
COMMIT = os.environ.get('MOBILE_HEAD_REV')
PR_TITLE = os.environ.get('GITHUB_PULL_TITLE', '')
BUILD_WORKER_TYPE = os.environ.get('BUILD_WORKER_TYPE', '')


# If we see this text inside a pull request title then we will not execute any tasks for this PR.
SKIP_TASKS_TRIGGER = '[ci skip]'


def create_task(name, description, command, generate_cot=False, scopes=None, treeherder=None, artifacts=None):
    return create_in_repo_task(
        name,
        description,
        full_command='./gradlew --no-daemon clean {}'.format(command),
        generate_cot=generate_cot,
        scopes=scopes,
        treeherder=treeherder,
        artifacts=artifacts,
    )


def create_in_repo_task(name, description, full_command, generate_cot=False, scopes=None,
                        treeherder=None, artifacts=None):
    scopes = [] if scopes is None else scopes
    treeherder = {} if treeherder is None else treeherder
    artifacts = {} if artifacts is None else artifacts

    return create_raw_task(name, description, BUILD_WORKER_TYPE, scopes, treeherder, {
        "features": {
            'taskclusterProxy': True,
            'chainOfTrust': generate_cot
        },
        "maxRunTime": 7200,
        "image": "mozillamobile/android-components:1.15",
        "command": [
            "/bin/bash",
            "--login",
            "-cx",
            "cd .. && git clone %s && cd reference-browser && git config advice.detachedHead false && git checkout %s && %s" % (
                REPO_URL, COMMIT, full_command)
        ],
        "artifacts": artifacts,
        "env": {
            "TASK_GROUP_ID": TASK_ID
        }
    })


def create_raw_task(name, description, workerType, scopes, treeherder, payload,
                    provisioner_id='aws-provisioner-v1', additional_dependencies=None):
    created = datetime.datetime.now()
    expires = taskcluster.fromNow('1 year')
    deadline = taskcluster.fromNow('1 day')

    return {
        "workerType": workerType,
        "taskGroupId": TASK_ID,
        "expires": taskcluster.stringDate(expires),
        "retries": 5,
        "created": taskcluster.stringDate(created),
        "tags": {},
        "priority": "lowest",
        "schedulerId": "taskcluster-github",
        "provisionerId": provisioner_id,
        "deadline": taskcluster.stringDate(deadline),
        "dependencies": [TASK_ID] + (additional_dependencies or []),
        "routes": [
            "tc-treeherder.v2.reference-browser.{}".format(COMMIT)
        ],
        "scopes": scopes,
        "requires": "all-completed",
        "payload": payload,
        "extra": {
            "treeherder": treeherder,
        },
        "metadata": {
            "name": name,
            "description": description,
            "owner": "android-components-team@mozilla.com",
            "source": "https://github.com/mozilla-mobile/android-components"
        }
    }


def create_variant_assemble_task(variant):
    return create_task(
        name="assemble: %s" % variant,
        description='Building and testing variant ' + variant,
        command='assemble{} && ls -R /build/reference-browser/'.format(variant.capitalize()),
        generate_cot=True,
        treeherder={
            'jobKind': 'build',
            'machine': {
              'platform': _craft_treeherder_platform_from_variant(variant),
            },
            'symbol': 'A',
            'tier': 1,
        },
        artifacts=_craft_artifacts_from_variant(variant),
    )


def create_dep_signing_task(artifacts):
    return create_raw_task(
        name='dep-sign',
        description='Dep-signing for performance testing',
        workerType='mobile-signing-dep-v1',
        scopes=[
            "project:mobile:reference-browser:releng:signing:format:autograph_apk_reference_browser",
            "project:mobile:reference-browser:releng:signing:cert:{}".format('dep-signing')
        ],
        treeherder={
            'jobKind': 'other',
            'machine': {
              'platform': 'android-all',
            },
            'symbol': 'Ns',
            'tier': 1,
        },
        payload={
            "maxRunTime": 3600,
            "upstreamArtifacts": [{
                'formats': ['autograph_apk_reference_browser'],
                'paths': [apk_path],
                'taskId': task_id,
                'taskType': 'build'
            } for (apk_path, task_id) in artifacts]
        },
        provisioner_id='scriptworker-prov-v1',
        additional_dependencies=[task_id for (_, task_id) in artifacts]
    )


def create_variant_test_task(variant):
    return create_task(
        name="test: %s" % variant,
        description='Building and testing variant ' + variant,
        command='test{}UnitTest && ls -R /build/reference-browser/'.format(variant.capitalize()),
        treeherder={
            'jobKind': 'test',
            'machine': {
              'platform': _craft_treeherder_platform_from_variant(variant),
            },
            'symbol': 'T',
            'tier': 1,
        },
    )

def _craft_treeherder_platform_from_variant(variant):
    architecture, build_type = _get_architecture_and_build_type_from_variant(variant)
    return 'android-{}-{}'.format(architecture, build_type)


def _craft_artifacts_from_variant(variant):
    arch, _ = _get_architecture_and_build_type_from_variant(variant)
    return {
        'public/target.{}.apk'.format(arch): {
            'type': 'file',
            'path': _craft_apk_full_path_from_variant(variant),
            'expires': taskcluster.stringDate(taskcluster.fromNow(lib.tasks.DEFAULT_EXPIRES_IN)),
        }
    }


def _craft_apk_full_path_from_variant(variant):
    architecture, build_type = _get_architecture_and_build_type_from_variant(variant)

    short_variant = variant[:-len(build_type)]
    shorter_variant = short_variant[:-len(architecture)]
    postfix = '-unsigned' if build_type == 'release' else ''

    return '/build/reference-browser/app/build/outputs/apk/{short_variant}/{build_type}/app-{shorter_variant}-{architecture}-{build_type}{postfix}.apk'.format(
        architecture=architecture,
        build_type=build_type,
        short_variant=short_variant,
        shorter_variant=shorter_variant,
        postfix=postfix
    )


def _get_architecture_and_build_type_from_variant(variant):
    variant = variant.lower()

    architecture = None
    if 'aarch64' in variant:
        architecture = 'aarch64'
    elif 'x86' in variant:
        architecture = 'x86'
    elif 'arm' in variant:
        architecture = 'arm'

    build_type = None
    if variant.endswith('debug'):
        build_type = 'debug'
    elif variant.endswith('release'):
        build_type = 'release'

    if not architecture or not build_type:
        raise ValueError(
            'Unsupported variant "{}". Found architecture, build_type: {}'.format(
                variant, (architecture, build_type)
            )
        )

    return architecture, build_type

def create_detekt_task():
    return create_task(
        name='detekt',
        description='Running detekt over all modules',
        command='detekt',
        treeherder={
            'jobKind': 'test',
            'machine': {
              'platform': 'lint',
            },
            'symbol': 'detekt',
            'tier': 1,
        }
    )


def create_ktlint_task():
    return create_task(
        name='ktlint',
        description='Running ktlint over all modules',
        command='ktlint',
        treeherder={
            'jobKind': 'test',
            'machine': {
              'platform': 'lint',
            },
            'symbol': 'ktlint',
            'tier': 1,
        }
    )


def create_lint_task():
    return create_task(
        name='lint',
        description='Running tlint over all modules',
        command='lint',
        treeherder={
            'jobKind': 'test',
            'machine': {
              'platform': 'lint',
            },
            'symbol': 'lint',
            'tier': 1,
        }
    )


def create_compare_locales_task():
    return create_in_repo_task(
        name='compare-locales',
        description='Validate strings.xml with compare-locales',
        full_command='pip install "compare-locales>=4.0.1,<5.0" && compare-locales --validate l10n.toml .',
        treeherder={
            'jobKind': 'test',
            'machine': {
              'platform': 'lint',
            },
            'symbol': 'compare-locale',
            'tier': 2,
        }
    )


def populate_chain_of_trust_required_but_unused_files():
    # These files are needed to keep chainOfTrust happy. However, they have no need for Reference
    # Browser # at the moment. For more details,
    # see: https://github.com/mozilla-releng/scriptworker/pull/209/files#r184180585

    for file_name in ('actions.json', 'parameters.yml'):
        with open(file_name, 'w') as f:
            f.truncate()
            f.write('{}\n')


if __name__ == "__main__":
    if SKIP_TASKS_TRIGGER in PR_TITLE:
        print("Pull request title contains", SKIP_TASKS_TRIGGER)
        print("Exit")
        exit(0)

    parser = argparse.ArgumentParser(
        description='Creates and submits a graph on taskcluster'
    )

    subparsers = parser.add_subparsers(dest='command')

    subparsers.add_parser('pr-open-or-push')
    subparsers.add_parser('master-push')

    command = parser.parse_args().command

    print("Fetching build variants from gradle")
    variants = lib.build_variants.from_gradle()

    if len(variants) == 0:
        print("Could not get build variants from gradle")
        sys.exit(2)

    print("Got variants: " + ' '.join(variants))
    queue = taskcluster.Queue({'baseUrl': 'http://taskcluster/queue/v1'})
    task_graph = TaskGraph(queue)

    arm_assemble_task_id = None
    aarch64_assemble_task_id = None
    for variant in variants:
        task_graph.schedule_new_task(create_variant_test_task(variant))
        assemble_task_id = task_graph.schedule_new_task(create_variant_assemble_task(variant))

        arch, build_type = _get_architecture_and_build_type_from_variant(variant)
        if build_type == 'debug' and arch == 'arm':
            arm_assemble_task_id = assemble_task_id
        elif build_type == 'debug' and arch == 'aarch64':
            aarch64_assemble_task_id = assemble_task_id

    if command == 'master-push':
        populate_chain_of_trust_required_but_unused_files()

        # autophone only supports arm and aarch64, so only sign/perftest those builds
        task_graph.schedule_new_task(create_dep_signing_task([
            ('public/target.apk', arm_assemble_task_id),
            ('public/target.apk', aarch64_assemble_task_id),
        ]))
        # raptor task will be added in follow-up

    task_graph.schedule_new_task(create_detekt_task())
    task_graph.schedule_new_task(create_ktlint_task())
    task_graph.schedule_new_task(create_compare_locales_task())
    task_graph.schedule_new_task(create_lint_task())

    raw_graph = task_graph.get_raw_graph()

    with open('task-graph.json', 'w') as f:
        json.dump(raw_graph, f)

    populate_chain_of_trust_required_but_unused_files()
