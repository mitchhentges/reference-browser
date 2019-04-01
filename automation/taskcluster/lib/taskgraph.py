from __future__ import print_function

import json

import taskcluster


class TaskGraph:
    def __init__(self, queue):
        self._task_graph = {}
        self._queue = queue

    def schedule_new_task(self, task):
        task_id = schedule_task(self._queue, task)

        self._task_graph[task_id] = {
            #'task': self._queue.task(task_id)
            'task': task_id
        }
        return task_id

    def get_raw_graph(self):
        return self._task_graph


def schedule_task(queue, task, task_id=taskcluster.slugId()):
    print("TASK", task_id)
    print(json.dumps(task, indent=4, separators=(',', ': ')))

    # result = queue.createTask(task_id, task)
    # print("RESULT", task_id)
    # print(json.dumps(result))
    return task_id
