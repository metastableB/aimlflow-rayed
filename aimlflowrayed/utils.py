import fnmatch
import click
import collections
import mlflow
import json
import time
import os.path
import numpy as np
import ray

from aim.sdk.repo import Repo
from ast import literal_eval
from tempfile import TemporaryDirectory
# from tqdm import tqdm
from ray.experimental.tqdm_ray import tqdm as tqdm

from aim import Run, Image, Text, Audio
from argparse import Namespace

IMAGE_EXTENSIONS = ('jpg', 'bmp', 'jpeg', 'png', 'gif', 'svg')
HTML_EXTENSIONS = ('html',)
TEXT_EXTENSIONS = (
    'txt',
    'log',
    'py',
    'js',
    'yaml',
    'yml',
    'json',
    'csv',
    'tsv',
    'md',
    'rst',
    'jsonnet',
)

# Audio is not handled in mlflow but including here just in case
AUDIO_EXTENSIONS = (
    'flac',
    'mp3',
    'wav',
)

@ray.remote
class RunHashCache:
    def __init__(self, repo_path, no_cache=False):
        """Maintains mlflow_run -> aim_run hash mapping on a file on disk. This
        way we won't create duplicate runs."""
        self._cache_path = os.path.join(repo_path, 'mlflow_logs_cache')
        self._needs_refresh = False

        if no_cache and os.path.exists(self._cache_path):
            os.remove(self._cache_path)
        try:
            with open(self._cache_path) as FS:
                self._cache = json.load(FS)
        except Exception:
            self._cache = {}

    def set(self, run_id, hashval):
        self.__setitem__(run_id, hashval)

    def get(self, run_id):
        return self._cache.get(run_id)

    def __setitem__(self, key: str, val: str):
        if not self._cache.get(key) == val:
            self._cache[key] = val
            self._needs_refresh = True

    def __getitem__(self, key: str):
        return self._cache[key]

    def refresh(self):
        """Write new run_id --> aim_run mapping to disk."""
        with open(self._cache_path, 'w') as FS:
            json.dump(self._cache, FS)


def get_mlflow_experiments(client, experiment):
    if experiment is None:
        experiments = client.search_experiments()
    else:
        try:
            ex = client.get_experiment(experiment)
        except mlflow.exceptions.MlflowException:
            ex = client.get_experiment_by_name(experiment)
        if not ex:
            raise RuntimeError(f'Could not find experiment with id or name "{experiment}"')
        experiments = (ex,)
    return experiments



@ray.remote
class CommitDriver:
    def __init__(self, repo_path, run_cache, num_connections):
        self.workers = []
        wfn = CommitWorker.options(**{'num_cpus': 1.0})
        for i in range(num_connections):
            w = wfn.remote(f'wrkr-{i}', repo_path, run_cache)
            self.workers.append(w)

    def commit(self, dummy_runs):
        """Divides runs almost equally among connections."""
        all_refs = []
        works = np.array_split(range(len(dummy_runs)), len(self.workers))
        for i in range(len(self.workers)):
            worker = self.workers[i]
            work = [dummy_runs[j] for j in works[i]]
            refs = worker.commit.remote(work)
            all_refs.append(refs)
        ray.get(all_refs)


@ray.remote
class CommitWorker:
    def __init__(self, workername, repo_path, run_cache):
        self._repo_path = repo_path
        self._workername = workername
        self._run_cache = run_cache

    def commit(self, runs):
        """Opens repo. sequentially commits runs. Closes repo"""
        repo_inst = Repo.from_path(self._repo_path)
        for drun in tqdm(runs, desc=f'{self._workername}: Commiting to db', total=len(runs)):
            aimrun = self.commit_dummy_run(drun, repo_inst)
            ray.get(self._run_cache.set.remote(drun.run_id, aimrun.hash))
        # close connection
        repo_inst.close()

    def commit_dummy_run(self, dummy, repo_inst):
        if hasattr(dummy, 'run_hash'):
            # Already exists in database.
            aim_run = Run(
                run_hash=dummy.run_hash,
                repo=repo_inst,
                system_tracking_interval=None,
                capture_terminal_logs=False,
                experiment=dummy.experiment,
            )
        else:
            aim_run = Run(
                repo=repo_inst,
                system_tracking_interval=None,
                capture_terminal_logs=False,
                experiment=dummy.experiment,
            )
        aim_run.name = dummy.run_name
        # Dump parameters
        for k, v in dummy.params.items():
            aim_run[k] = v
        # Dump metrics
        for k, v in dummy.metrics.items():
            metric_history = v
            for m in metric_history:
                aim_run.track(m.value, step=m.step, name=m.key)
        return aim_run


def get_dummyrun(run_id, run_name, experiment_name, run_cache):
    run_ = ray.get(run_cache.get.remote(run_id))
    if run_:
        dummyrun = Namespace(
            run_id=run_id,
            run_name=run_name,
            run_hash=run_,
            experiment=experiment_name,
            metrics = {}, params = {}, artifacts = {},
        )
    else:
        dummyrun = Namespace(
            run_id=run_id,
            run_name=run_name,
            experiment=experiment_name,
            metrics = {}, params = {}, artifacts = {},
        )
    return dummyrun

def convert_existing_logs(repo_path, tracking_uri, experiment=None,
                          excluded_artifacts=None):
    client = mlflow.tracking.client.MlflowClient(tracking_uri=tracking_uri)
    experiments = get_mlflow_experiments(client, experiment)
    run_cache = RunHashCache.remote(repo_path)
    db_driver = CommitDriver.remote(repo_path, run_cache, num_connections=4)
    for ex in tqdm(experiments, desc=f'Parsing mlflow experiments in {tracking_uri}', total=len(experiments)):
        runs = client.search_runs(ex.experiment_id)
        dummy_refs = []
        for run in runs:
            kwargs = {
                'run_cache': run_cache, 'tracking_uri': tracking_uri,
                'mlflow_runid': run.info.run_id,
                'mlflow_runname': run.info.run_name, 'exp_name': ex.name,
                'excluded_artifacts': excluded_artifacts,
            }
            dummy_ref = fetch_run_details.options(**{'num_cpus': 1.0}).remote(**kwargs)
            dummy_refs.append(dummy_ref)
        _dummy_runs, not_ready = [], dummy_refs
        for _ in tqdm(runs, desc=f'{ex.name}: Parsing mlflow runs', total=len(runs)):
            ready, not_ready = ray.wait(not_ready, num_returns=1)
            _dummy_runs.extend(ray.get(ready))
        ray.get(db_driver.commit.remote(_dummy_runs))
        ray.get(run_cache.refresh.remote())
        print("Done with experiment: %s", ex.name)
    print("Done with everything.")



@ray.remote
def fetch_run_details(run_cache, tracking_uri, mlflow_runid, mlflow_runname, exp_name, excluded_artifacts=None):
    client = mlflow.tracking.client.MlflowClient(tracking_uri=tracking_uri)
    aim_run = get_dummyrun(mlflow_runid, mlflow_runname, exp_name, run_cache)
    mlflow_run = client.get_run(mlflow_runid)
    collect_run_params(aim_run, mlflow_run)
    collect_metrics(aim_run, mlflow_run, client)
    # collect_artifacts(aim_run, mlflow_run, client, excluded_artifacts)
    return aim_run

# def get_aim_run(repo_inst, run_id, run_name, experiment_name, run_cache):
#     run_ = ray.get(run_cache.get.remote(run_id))
#     print("run hash", run_)
#     if run_:
#         aim_run = Run(
#             run_hash=run_,
#             repo=repo_inst,
#             system_tracking_interval=None,
#             capture_terminal_logs=False,
#             experiment=experiment_name,
#         )
#     else:
#         print("CREATING AIMRUN", repo_inst)
#         aim_run = Run(
#             repo=repo_inst,
#             system_tracking_interval=None,
#             capture_terminal_logs=False,
#             experiment=experiment_name,
#         )
#         print("Getting aim_run", aim_run.hash)
#         ray.get(run_cache.set.remote(run_id, aim_run.hash))
#     aim_run.name = run_name
#     return aim_run


def collect_run_params(dummyrun, mlflow_run):
    dummyrun.params['mlflow_run_id'] = mlflow_run.info.run_id
    dummyrun.params['mlflow_run_id'] = mlflow_run.info.run_id
    dummyrun.params['mlflow_experiment_id'] = mlflow_run.info.experiment_id
    dummyrun.params['description'] = mlflow_run.data.tags.get("mlflow.note.content")
    # Collect params & tags
    # MLflow provides "string-ified" params values and we try to revert that
    dummyrun.params['params'] = _map_nested_dicts(_try_parse_str, mlflow_run.data.params)
    dummyrun.params['tags'] = {
        k: v for k, v in mlflow_run.data.tags.items() if not k.startswith('mlflow')
    }

def collect_metrics(dummyrun, mlflow_run, mlflow_client, timestamp=None):
    for key in mlflow_run.data.metrics.keys():
        metric_history = mlflow_client.get_metric_history(mlflow_run.info.run_id, key)
        if timestamp:
            metric_history = list(filter(lambda m: m.timestamp >= timestamp, metric_history))

        dummyrun.metrics[key] = metric_history



# def collect_artifacts(aim_run, mlflow_run, mlflow_client, exclude_artifacts):
#     return
#     if '*' in exclude_artifacts:
#         return
#
#     run_id = mlflow_run.info.run_id
#
#     artifacts_cache_key = '_mlflow_artifacts_cache'
#     artifacts_cache = aim_run.meta_run_tree.get(artifacts_cache_key) or []
#
#     __html_warning_issued = False
#     with TemporaryDirectory(prefix=f'mlflow_{run_id}_') as temp_path:
#         artifact_loc_stack = [None]
#         while artifact_loc_stack:
#             loc = artifact_loc_stack.pop()
#             artifacts = mlflow_client.list_artifacts(run_id, path=loc)
#
#             for file_info in artifacts:
#                 if file_info.is_dir:
#                     artifact_loc_stack.append(file_info.path)
#                     continue
#
#                 if file_info.path in artifacts_cache:
#                     continue
#                 else:
#                     artifacts_cache.append(file_info.path)
#
#                 if exclude_artifacts:
#                     exclude = False
#                     for expr in exclude_artifacts:
#                         if fnmatch.fnmatch(file_info.path, expr):
#                             exclude = True
#                             break
#                     if exclude:
#                         continue
#
#                 downloaded_path = mlflow_client.download_artifacts(run_id, file_info.path, dst_path=temp_path)
#                 if file_info.path.endswith(HTML_EXTENSIONS):
#                     if not __html_warning_issued:
#                         click.secho(
#                             'Handler for html file types is not yet implemented.', fg='yellow'
#                         )
#                         __html_warning_issued = True
#                     continue
#                 elif file_info.path.endswith(IMAGE_EXTENSIONS):
#                     aim_object = Image
#                     kwargs = dict(
#                         image=downloaded_path,
#                         caption=file_info.path
#                     )
#                     content_type = 'image'
#                 elif file_info.path.endswith(TEXT_EXTENSIONS):
#                     with open(downloaded_path) as fh:
#                         content = fh.read()
#                     aim_object = Text
#                     kwargs = dict(
#                         text=content
#                     )
#                     content_type = 'text'
#                 elif file_info.path.endswith(AUDIO_EXTENSIONS):
#                     audio_format = os.path.splitext(file_info.path)[1].lstrip('.')
#                     aim_object = Audio
#                     kwargs = dict(
#                         data=downloaded_path,
#                         caption=file_info.path,
#                         format=audio_format
#                     )
#                     content_type = 'audio'
#                 else:
#                     click.secho(
#                         f'Unresolved or unsupported type for artifact {file_info.path}', fg='yellow'
#                     )
#                     continue
#
#                 try:
#                     item = aim_object(**kwargs)
#                 except Exception as exc:
#                     click.echo(
#                         f'Could not convert artifact {file_info.path} into aim object - {exc}', err=True
#                     )
#                     continue
#                 aim_run.track(item, name=loc or 'root', context={'type': content_type})
#
#             aim_run.meta_run_tree[artifacts_cache_key] = artifacts_cache




def _wait_forever(watcher):
    try:
        while True:
            time.sleep(24 * 60 * 60)  # sleep for a day
    except KeyboardInterrupt:
        watcher.stop()


def _map_nested_dicts(fun, tree):
    if isinstance(tree, collections.abc.Mapping):
        return {k: _map_nested_dicts(fun, subtree) for k, subtree in tree.items()}
    else:
        return fun(tree)


def _try_parse_str(s):
    assert isinstance(s, str), f'Expected a string, got {s} of type {type(s)}'
    try:
        return literal_eval(s.strip())
    except:  # noqa: E722
        return s
