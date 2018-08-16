#!/usr/bin/env python3
import boto3
from pprint import pprint
import toml
import os
import syslog

# Purpose - to be ran with cron every minute and terminate abandoned spot instances.


def main():
    # Regions our spot instances may be running in.
    regions = ['us-east-1', 'us-east-2']
    for region in regions:
        ec2 = boto3.resource(
            'ec2',
            region_name=region,
            aws_access_key_id=os.environ['AWS_KEY'],
            aws_secret_access_key=os.environ['AWS_SECRET'])
        # All spot instances running.
        all_ci_instances = set(
            ec2.instances.filter(Filters=[
                {
                    'Name': 'instance.group-name',
                    'Values': ['docker-machine']
                },
                {
                    'Name': 'instance-state-name',
                    'Values': ['running']
                },
            ]))
        # All spot instances that are NOT abandoned.
        all_functional_ci_instances = set(
            ec2.instances.filter(Filters=[
                {
                    'Name': 'instance.group-name',
                    'Values': ['docker-machine']
                },
                {
                    'Name': 'instance-state-name',
                    'Values': ['running']
                },
                {
                    'Name': 'tag-key',
                    'Values': ['Name']
                },
            ]))

        # This right here is how the bug somewhere between docker-machine and gitlab-runner expresses itself.
        horde = all_ci_instances - all_functional_ci_instances

        if len(horde) == 0:
            syslog.syslog("spot_sniper.py - No abandoned spot instances.")

        for zombie in horde:
            kill_with_fire(zombie)


def kill_with_fire(zombie):
    syslog.syslog(
        f"spot_sniper.py - Terminating zombie spot instance {zombie.id}.")
    zombie.terminate()


if __name__ == '__main__':
    main()
