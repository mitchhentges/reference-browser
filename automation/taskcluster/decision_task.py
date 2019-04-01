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

import arrow
import taskcluster

import lib.build_variants
from lib.taskgraph import TaskGraph, schedule_task

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
                    provisioner_id='aws-provisioner-v1', additional_dependencies=None,
                    additional_routes=None):
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
        ] + (additional_routes or []),
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


def create_nightly_assemble_task(architectures):
    artifacts = {'public/target.{}.apk'.format(arch): {
        "type": 'file',
        "path": "/build/reference-browser/app/build/outputs/apk/geckoNightly{}/release/"
                "app-geckoNightly-{}-release-unsigned.apk".format(arch.capitalize(), arch),
        "expires": taskcluster.stringDate(taskcluster.fromNow('1 year')),
    } for arch in architectures}

    return create_in_repo_task(
        name='(Reference Browser) Build task',
        description='Build Reference Browser from source code.',
        full_command='python automation/taskcluster/helper/get-secret.py '
                     '-s project/mobile/reference-browser/sentry -k dsn -f .sentry_token '
                     '&& ./gradlew --no-daemon -PcrashReportEnabled=true -Ptelemetry=true '
                     'clean test assembleRelease',
        generate_cot=True,
        artifacts=artifacts,
        scopes=['secrets:get:project/mobile/reference-browser/sentry'],
        treeherder={
            'jobKind': 'build',
            'machine': {
              'platform': 'android-all',
            },
            'symbol': 'NA',
            'tier': 1,
        }
    )


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


def create_nightly_signing_task(architectures, build_task_id, date, is_staging):
    signing = 'dep-signing' if is_staging else 'release-signing'
    index_release = 'staging-signed-nightly' if is_staging else 'signed-nightly'
    worker_type = 'mobile-signing-dep-v1' if is_staging else 'mobile-signing-v1'

    return create_raw_task(
        name='(Reference Browser) Signing task',
        description='Sign release builds of Reference Browser',
        workerType=worker_type,
        scopes=[
            'project:mobile:reference-browser:releng:signing:cert:{}'.format(signing),
            'project:mobile:reference-browser:releng:signing:format:'
            'autograph_apk_reference_browser',
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
            'maxRunTime': 3600,
            "upstreamArtifacts": [{
                'formats': ['autograph_apk_reference_browser'],
                'paths': ['public/target.{}.apk'.format(arch) for arch in architectures],
                'taskId': build_task_id,
                'taskType': 'build'
            }]
        },
        provisioner_id='scriptworker-prov-v1',
        additional_dependencies=[build_task_id],
        additional_routes=[
            'index.project.mobile.reference-browser.{}.nightly.{}.{}.{}.latest'
                .format(index_release, date.year, date.month, date.day),
            'index.project.mobile.reference-browser.{}.nightly.{}.{}.{}.revision.{}'
                .format(index_release, date.year, date.month, date.day, COMMIT),
            'index.project.mobile.reference-browser.{}.nightly.latest'
                .format(index_release),
        ]
    )


def create_google_push_task(architectures, sign_task_id, is_staging):
    worker_type = 'mobile-pushapk-dep-v1' if is_staging else 'mobile-pushapk-v1'

    return create_raw_task(
        name='(Reference Browser) Push task',
        description='Upload signed release builds of Reference Browser to Google Play',
        workerType=worker_type,
        scopes=['project:mobile:reference-browser:releng:googleplay:product:reference-browser{}'
                    .format(':dep' if is_staging else '')],
        treeherder={
            'jobKind': 'other',
            'machine': {
              'platform': 'android-all',
            },
            'symbol': 'gp',
            'tier': 1,
        },
        payload={
            'commit': True,
            'google_play_track': 'nightly',
            'upstreamArtifacts': [{
                'paths': ['public/target.{}.apk'.format(arch) for arch in architectures],
                'taskId': sign_task_id,
                'taskType': 'signing',
            }],
        },
        provisioner_id='scriptworker-prov-v1',
        additional_dependencies=[sign_task_id],
    )


# For GeckoView, upload nightly (it has release config) by default, all Release builds have WV
def create_nimbledroid_task():
    return create_in_repo_task(
        name='(RB for Android) Upload Debug APK to Nimbledroid',
        description='Upload APKs to Nimbledroid for performance measurement and tracking.',
        full_command='./gradlew --no-daemon clean assembleDebug '
                     '&& python automation/taskcluster/upload_apk_nimbledroid.py',
        scopes=['secrets:get:project/mobile/reference-browser/nimbledroid'],
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
            'expires': taskcluster.stringDate(taskcluster.fromNow('1 year')),
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


def nightly(queue, date_string, is_staging):
    date = arrow.get(date_string)
    task_graph = TaskGraph(queue)
    architectures = ['x86', 'arm', 'aarch64']

    build_task_id = task_graph.schedule_new_task(create_nightly_assemble_task(architectures))
    sign_task_id = task_graph.schedule_new_task(create_nightly_signing_task(
        architectures, build_task_id, date, is_staging))
    task_graph.schedule_new_task(create_google_push_task(architectures, sign_task_id, is_staging))

    if not is_staging:
        task_graph.schedule_new_task(create_nimbledroid_task())

    raw_graph = task_graph.get_raw_graph()
    populate_chain_of_trust_required_but_unused_files(raw_graph)


def master_push(queue):
    task_graph = TaskGraph(queue)

    task_graph.schedule_new_task(create_detekt_task())
    task_graph.schedule_new_task(create_ktlint_task())
    task_graph.schedule_new_task(create_compare_locales_task())
    task_graph.schedule_new_task(create_lint_task())

    arm_assemble_task_id = None
    aarch64_assemble_task_id = None
    for variant in lib.build_variants.from_gradle():
        task_graph.schedule_new_task(create_variant_test_task(variant))
        assemble_task_id = task_graph.schedule_new_task(create_variant_assemble_task(variant))

        arch, build_type = _get_architecture_and_build_type_from_variant(variant)
        if build_type == 'debug' and arch == 'arm':
            arm_assemble_task_id = assemble_task_id
        elif build_type == 'debug' and arch == 'aarch64':
            aarch64_assemble_task_id = assemble_task_id

    # autophone only supports arm and aarch64, so only sign/perftest those builds
    task_graph.schedule_new_task(create_dep_signing_task([
        ('public/target.apk', arm_assemble_task_id),
        ('public/target.apk', aarch64_assemble_task_id),
    ]))
    # raptor task will be added in follow-up

    raw_graph = task_graph.get_raw_graph()
    populate_chain_of_trust_required_but_unused_files(raw_graph)


def pr_open_or_push(queue):
    schedule_task(queue, create_detekt_task())
    schedule_task(queue, create_ktlint_task())
    schedule_task(queue, create_compare_locales_task())
    schedule_task(queue, create_lint_task())

    for variant in lib.build_variants.from_gradle():
        schedule_task(queue, create_variant_test_task(variant))
        schedule_task(queue, create_variant_assemble_task(variant))


def populate_chain_of_trust_required_but_unused_files(raw_graph):
    # These files are needed to keep chainOfTrust happy. However, they have no need for Reference
    # Browser # at the moment. For more details,
    # see: https://github.com/mozilla-releng/scriptworker/pull/209/files#r184180585

    for file_name in ('actions.json', 'parameters.yml'):
        with open(file_name, 'w') as f:
            f.truncate()
            f.write('{}\n')

    with open('task-graph.json', 'w') as f:
        json.dump(raw_graph, f)


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
    release_command = subparsers.add_parser('release')

    release_command.add_argument('--date', dest="date", action="store", required=True,
                                 help="ISO8601 timestamp for build")
    release_command.add_argument('--staging', action="store_true",
                                 help="Perform a staging build (use dep workers, "
                                      "don't communicate with Google Play) ")

    result = parser.parse_args()
    taskcluster_queue = taskcluster.Queue({'baseUrl': 'http://taskcluster/queue/v1'})

    if result.command == 'release':
        nightly(taskcluster_queue, result.date, result.staging)
    elif result.command == 'master-push':
        master_push(taskcluster_queue)
    else:
        pr_open_or_push(taskcluster_queue)
