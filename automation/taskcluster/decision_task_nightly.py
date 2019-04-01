# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""
Decision task for nightly releases.
"""

from __future__ import print_function

import argparse
import arrow
import json

from lib.taskgraph import TaskGraph
import lib.tasks
import os
import taskcluster

TASK_ID = os.environ.get('TASK_ID')
SCHEDULER_ID = os.environ.get('SCHEDULER_ID')
GITHUB_HTTP_REPOSITORY = os.environ.get('MOBILE_HEAD_REPOSITORY')
HEAD_REV = os.environ.get('MOBILE_HEAD_REV')

BUILDER = lib.tasks.TaskBuilder(
    task_id=TASK_ID,
    commit=HEAD_REV,
    owner="android-components-team@mozilla.com",
    source='{}/raw/{}/.taskcluster.yml'.format(GITHUB_HTTP_REPOSITORY, HEAD_REV),
    scheduler_id=SCHEDULER_ID,
    build_worker_type=os.environ.get('BUILD_WORKER_TYPE'),
)


def generate_build_task(architectures):
    artifacts = {'public/target.{}.apk'.format(arch): {
        "type": 'file',
        "path": "/build/reference-browser/app/build/outputs/apk/geckoNightly{}/release/"
                "app-geckoNightly-{}-release-unsigned.apk".format(arch.capitalize(), arch),
        "expires": taskcluster.stringDate(taskcluster.fromNow('1 year')),
    } for arch in architectures}

    checkout = 'git clone {} && cd reference-browser && git checkout {}'.format(GITHUB_HTTP_REPOSITORY, HEAD_REV)

    return BUILDER.build_task(
        name="(Reference Browser) Build task",
        description="Build Reference Browser from source code.",
        command=('cd .. && ' + checkout +
                 ' && python automation/taskcluster/helper/get-secret.py'
                 ' -s project/mobile/reference-browser/sentry -k dsn -f .sentry_token'
                 ' && ./gradlew --no-daemon -PcrashReportEnabled=true -Ptelemetry=true clean test assembleRelease'),
        features={
            "chainOfTrust": True,
            "taskclusterProxy": True
        },
        artifacts=artifacts,
        scopes=[
            "secrets:get:project/mobile/reference-browser/sentry"
        ]
    )


def generate_signing_task(build_task_id, architectures, date, is_staging):
    artifacts = ["public/target.{}.apk".format(arch) for arch in architectures]

    index_release = 'staging-signed-nightly' if is_staging else 'signed-nightly'
    routes = [
        "index.project.mobile.reference-browser.{}.nightly.{}.{}.{}.latest".format(index_release, date.year, date.month, date.day),
        "index.project.mobile.reference-browser.{}.nightly.{}.{}.{}.revision.{}".format(index_release, date.year, date.month, date.day, HEAD_REV),
        "index.project.mobile.reference-browser.{}.nightly.latest".format(index_release),
    ]
    scopes = [
        "project:mobile:reference-browser:releng:signing:format:autograph_apk_reference_browser",
        "project:mobile:reference-browser:releng:signing:cert:{}".format(
            'dep-signing' if is_staging else 'release-signing')
    ]

    return BUILDER.craft_signing_task(
        build_task_id,
        name="(Reference Browser) Signing task",
        description="Sign release builds of Reference Browser",
        apks=artifacts,
        scopes=scopes,
        routes=routes,
        signing_format='autograph_apk_reference_browser',
        is_staging=is_staging
    )


def generate_push_task(signing_task_id, architectures, is_staging):
    artifacts = ["public/target.{}.apk".format(arch) for arch in architectures]

    return taskcluster.slugId(), BUILDER.craft_push_task(
        signing_task_id,
        name="(Reference Browser) Push task",
        description="Upload signed release builds of Reference Browser to Google Play",
        apks=artifacts,
        scopes=[
            "project:mobile:reference-browser:releng:googleplay:product:reference-browser{}".format(':dep' if is_staging else '')
        ],
        is_staging=is_staging
    )


# For GeckoView, upload nightly (it has release config) by default, all Release builds have WV
def generate_upload_apk_nimbledroid_task(build_task_id):
    checkout = 'git clone {} && cd reference-browser && git checkout {}'.format(GITHUB_HTTP_REPOSITORY, HEAD_REV)
    return taskcluster.slugId(), BUILDER.craft_upload_apk_nimbledroid_task(
        build_task_id,
        name="(RB for Android) Upload Debug APK to Nimbledroid",
        description="Upload APKs to Nimbledroid for performance measurement and tracking.",
        command=(#'echo "--" > .adjust_token'
                 'cd .. && ' + checkout +
                 ' && ./gradlew --no-daemon clean assembleDebug'
                 ' && python automation/taskcluster/upload_apk_nimbledroid.py'),
        dependencies= [build_task_id],
        scopes=["secrets:get:project/mobile/reference-browser/nimbledroid"],
)


def populate_chain_of_trust_required_but_unused_files():
    # These files are needed to keep chainOfTrust happy. However, they have no need for Reference Browser
    # at the moment. For more details, see: https://github.com/mozilla-releng/scriptworker/pull/209/files#r184180585

    for file_name in ('actions.json', 'parameters.yml'):
        with open(file_name, 'w') as f:
            json.dump({}, f)


def nightly(date_string, is_staging):
    queue = taskcluster.Queue({'baseUrl': 'http://taskcluster/queue/v1'})
    date = arrow.get(date_string)

    task_graph = TaskGraph(queue)
    architectures = ['x86', 'arm', 'aarch64']

    build_task_id = task_graph.schedule_new_task(generate_build_task(architectures))
    sign_task_id = task_graph.schedule_new_task(
        generate_signing_task(build_task_id, architectures, date, is_staging))
    task_graph.schedule_new_task(generate_push_task(sign_task_id, architectures, is_staging))
    task_graph.schedule_new_task(generate_upload_apk_nimbledroid_task(build_task_id))

    raw_graph = task_graph.get_raw_graph()
    print(json.dumps(raw_graph, indent=4, separators=(',', ': ')))

    with open('task-graph.json', 'w') as f:
        json.dump(raw_graph, f)

    populate_chain_of_trust_required_but_unused_files()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Create a release pipeline (build, sign, publish) on taskcluster.')

    parser.add_argument('--date', dest="date", action="store", help="ISO8601 timestamp for build")
    parser.add_argument('--staging', action="store_true", help="Perform a staging build (use dep workers, "
                                                               "don't communicate with Google Play) ")

    result = parser.parse_args()
    nightly(result.date, result.staging)
