#!/usr/bin/env python
import docker
import requests
import os
from sh import wall
from sh import echo
from pathlib import Path

# The purpose of this script is to be ran as a cron to cancel and restart jobs on spot instances marked for termination.


def main():
    if to_be_terminated():
        terminate_jobs()


def to_be_terminated():
    # This function determines if termination needs to be addressed.
    if not Path("/clean-exit-attempted").is_file():
        try:
            resp = requests.get(
                'http://169.254.169.254/latest/meta-data/spot/termination-time')
            resp.raise_for_status()
            wall(echo('Restarting job as this instance will be terminated.'))
            return True
        except:
            return False
    else:
        return False


def wall_all(container, msg):
    # This function uses wall to message all ttys.
    wall(echo(msg))
    container.exec_run(f'sh -c "echo \'{msg}\' | wall"')


def terminate_jobs():
    # This function cancels the job running on the spot instance marked for termination and re-queues the job in Gitlab.
    # It will run a cleanup script, /exit-cleanly.sh, if it is available.
    client = docker.from_env()
    Path("/clean-exit-attempted").touch()
    for container in client.containers.list(filters={
            'status': 'running',
    }):
        try:
            jid = container.exec_run('sh -c "echo ${CI_JOB_ID?"NOJOB"}"')[
                1].decode('utf-8').strip('\n')
            pid = container.exec_run('sh -c "echo ${CI_PROJECT_ID?"NOJOB"}"')[
                1].decode('utf-8').strip('\n')
            if (pid != "NOJOB") and (jid != "NOJOB"):
                job_container = container
                container.exec_run('sh -c "/exit-cleanly.sh"')
        except:
            wall_all(
                job_container,
                f"Giving on on clean exit and restarting job {jid} of project {pid}."
            )
            pass

    kill_url = f"{gitlab_api}/projects/{pid}/jobs/{jid}/cancel"
    retry_url = f"{gitlab_api}/projects/{pid}/jobs/{jid}/retry?scope[]=pending&scope[]=running"
    auth_header = {'PRIVATE-TOKEN': os.environ.get('GITLAB_TOKEN')}

    killed = False
    tries = 20
    while not killed and tries > 0:
        try:
            tries -= 1
            resp = requests.post(kill_url, headers=auth_header)
            #gitlab status code
            print(resp.json()['id'])
            killed = True
        except:
            wall_all(job_container, 'Failed to cancel job, retrying.')
            pass

    if killed:
        wall_all(job_container, "Cancellation successful.")

    retried = False
    tries = 20
    while not retried and tries > 0:
        try:
            tries -= 1
            resp = requests.post(retry_url, headers=auth_header)
            #gitlab status code
            print(resp.json()['id'])
            retried = True
        except:
            wall_all(job_container, 'Failed to restart job, retrying.')
            pass

    if retried:
        wall_all(job_container, "Restarted job - That's all folks!!!.")


if __name__ == '__main__':
    gitlab_api = "https://example.com/api/v4"
    main()
