import boto3
from functools import total_ordering
import datetime
import syslog
import requests
import os
from sh import sed
from sh import systemctl
import time

# Purpose - reconfigure Gitlab CI to use the cheapest usable instance type, considering region, zone and availability.


def main():
    ## Configuration parameters.
    # Instance types to consider using.
    instances = ['m5.xlarge', 'm4.xlarge', 'c4.2xlarge', 'c5.2xlarge']
    # Regions to consider using
    regions = ['us-east-1', 'us-east-2']
    # Zones to consider using.
    zones = ['a', 'b', 'c', 'd', 'e', 'f']
    # AMIs to use in each region being considered.
    region_amis = {'us-east-1': 'ami-5bc0cf24', 'us-east-2': 'ami-3de9d358'}
    # Max Bid price to use when assessing availability.
    max_bid = 0.08
    # Number of instances to provision when assessing availability.
    test_instances = 3
    # Number of times to check AWS to see if instances are available while assessing availability.
    wait_retries = 2
    if safe_to_update_config():
        inst_confs = get_price_list(instances, regions, zones)
        for conf in inst_confs:
            if spot_test(conf.region, conf.zone, region_amis[conf.region],
                         conf.instance, test_instances, max_bid, wait_retries):
                if safe_to_update_config():
                    syslog.syslog(f"min_bid_price.py - Min Price: {conf}")
                    update_config(conf, region_amis)
                    break
                else:
                    syslog.syslog(
                        f"min_bid_price.py - Cannot update config as jobs are running."
                    )
                    break
            else:
                syslog.syslog(
                    f"min_bid_price.py - {conf} failed provisioning check.")
    else:
        syslog.syslog(
            f"min_bid_price.py - Cannot update config as jobs are running.")


def update_config(next_inst, region_amis):
    # Update /etc/gitlab-runner/config.toml with new configuration.
    sed("-i", f"s/amazonec2-zone=[a-f]/amazonec2-zone={next_inst.zone}/g",
        "/etc/gitlab-runner/config.toml")
    sed(
        "-i",
        f"s/amazonec2-ami=ami-[a-z0-9]*/amazonec2-ami={region_amis[next_inst.region]}/g",
        "/etc/gitlab-runner/config.toml")
    sed(
        "-i",
        f"s/amazonec2-instance-type=[a-z0-9]*.[a-z0-9]*/amazonec2-instance-type={next_inst.instance}/g",
        "/etc/gitlab-runner/config.toml")
    systemctl("restart", "gitlab-runner")
    syslog.syslog(f"min_bid_price.py - Moved CI to {next_inst}")


def get_price_list(instances, regions, zones):
    # Create a sorted list of instance_profile objects.
    price_list = []
    for region in regions:
        client = boto3.client(
            'ec2',
            region_name=region,
            aws_access_key_id=os.environ['AWS_KEY'],
            aws_secret_access_key=os.environ['AWS_SECRET'])
        for instance_type in instances:
            for zone in zones:
                price = instance_profile(instance_type, region, zone)
                if price.determine_price(client):
                    price_list.append(price)
    price_list.sort()
    return price_list


def safe_to_update_config():
    ## Determine if configuration can be updated by:
    # Making sure no non-abandoned spot instances are running.
    # Making sure no CI jobs are running.
    auth_header = {'PRIVATE-TOKEN': os.environ['GITLAB_TOKEN']}
    try:
        resp = requests.get(
            'https://example.com/api/v4/runners/4/jobs?status=running',
            headers=auth_header)
        resp.raise_for_status()
    except:
        syslog.syslog(
            'min_bid_price.py - Cannot get runner status from example.com. Something up?'
        )
        return False
    if len(resp.json()) != 0:
        return False
    else:
        instances_running = "/root/.docker/machine/machines"
        if os.listdir(instances_running):
            return False
        return True


@total_ordering
class instance_profile:
    # This class holds instance pricing information.
    # It uses total_ordering to simplify sorting by price.
    def __init__(self, instance, region, zone):
        self.instance = instance
        self.region = region
        self.zone = zone
        self.price = None

    def determine_price(self, client):
        try:
            resp = client.describe_spot_price_history(
                InstanceTypes=[self.instance],
                MaxResults=1,
                ProductDescriptions=['Linux/UNIX (Amazon VPC)'],
                AvailabilityZone=self.region + self.zone)
            self.price = float(resp['SpotPriceHistory'][0]['SpotPrice'])
            return True
        except:
            return False

    def __eq__(self, other):
        if self.price == other.price:
            return True
        else:
            return False

    def __gt__(self, other):
        if self.price > other.price:
            return True
        else:
            return False

    def __str__(self):
        if self.price is None:
            return f"No price for {self.instance} {self.region}{self.zone}"
        else:
            return f"{self.instance} at {self.region}{self.zone} costing {self.price} at {datetime.datetime.now()}"


def spot_test(region, availability_zone, ami, instance_type, instances, max_bid,
              wait_retries):
    # This function determines if an instance_profile has enough capacity to be usable.
    client = boto3.client(
        'ec2',
        region_name=region,
        aws_access_key_id=os.environ['AWS_KEY'],
        aws_secret_access_key=os.environ['AWS_SECRET'])
    req_ids = spot_up(client, instances, max_bid, ami, availability_zone,
                      region, instance_type)
    usable_config = check_type_in_az(client, wait_retries, req_ids)
    spot_stop(client, req_ids)
    spot_down(client, req_ids)
    if usable_config:
        syslog.syslog(
            f"min_bid_price.py - {region}{availability_zone} {instance_type} wins as it spins up {instances} instances in {wait_retries*5} seconds at max_bid {max_bid}."
        )
        return True
    else:
        syslog.syslog(
            f"min_bid_price.py - {region}{availability_zone} {instance_type} loses as it fails to spins up {instances} instances in {wait_retries*5} seconds at max_bid {max_bid}."
        )
        return False


def spot_up(client, instances, max_bid, ami, availability_zone, region,
            instance_type):
    # This function requests spot instances and returns the IDs of those requests.
    responses = []
    for i in range(instances):
        responses.append(
            client.request_spot_instances(
                LaunchSpecification={
                    'ImageId': ami,
                    'InstanceType': instance_type,
                    'Placement': {
                        'AvailabilityZone': region + availability_zone,
                    },
                    'SecurityGroups': [
                        'docker-machine',
                    ],
                },
                SpotPrice=str(max_bid),
                Type='one-time',
                InstanceInterruptionBehavior='terminate'))
    return [
        x["SpotInstanceRequests"][0]["SpotInstanceRequestId"] for x in responses
    ]


def spot_stop(client, req_ids):
    # This function stops spot instance requests.
    cancellations = (
        client.cancel_spot_instance_requests(SpotInstanceRequestIds=[x])[
            "CancelledSpotInstanceRequests"][0]["State"] == "cancelled"
        for x in req_ids)
    while False in cancellations:
        print(
            f"min_bid_price.py - Failed to cancel all spot requests, retrying")
        time.sleep(5)
        cancellations = (
            client.cancel_spot_instance_requests(SpotInstanceRequestIds=[x])[
                "CancelledSpotInstanceRequests"][0]["State"] == "cancelled"
            for x in req_ids)


def spot_down(client, req_ids):
    # This function terminates provisioned spot instances.
    instances = [
        client.describe_spot_instance_requests(SpotInstanceRequestIds=[x])
        for x in req_ids
    ]
    terminate_ids = []
    for x in instances:
        try:
            terminate_ids.append(x["SpotInstanceRequests"][0]["InstanceId"])
        except KeyError:
            pass
    if len(terminate_ids) > 0:
        client.terminate_instances(InstanceIds=terminate_ids)


def check_type_in_az(client, wait_retries, req_ids):
    ## This function determines if an instance_profile is usable by checking the status of requests made using it.
    statuses = spot_req_status(client, req_ids)
    while wait_retries > 0 and req_status_check(statuses) is None:
        wait_retries -= 1
        statuses = spot_req_status(client, req_ids)
        time.sleep(5)
    if req_status_check(statuses) is None:
        return False
    else:
        return req_status_check(statuses)


def spot_req_status(client, req_ids):
    # This function returns the status of spot requests.
    return [
        client.describe_spot_instance_requests(
            SpotInstanceRequestIds=[x])["SpotInstanceRequests"][0]["Status"]
        for x in req_ids
    ]


def req_status_check(statuses):
    # This function determines if an list of request statuses are all successful, pending, or not all successful.
    for x in statuses:
        if (x["Code"] == "pending-evaluation") or (
                x["Code"] == "pending-fulfillment"):
            return None
        elif x["Code"] != "fulfilled":
            syslog.syslog(f"Fail req_status: {x['Code']}")
            return False
        else:
            pass
    return True


if __name__ == '__main__':
    main()
