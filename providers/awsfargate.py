"""AWS ECS on Fargate.

Expects an existing task definition whose container (AWS_FARGATE_CONTAINER_NAME)
runs RUNTIME_IMAGE; per-world environment, CPU and memory are passed as
run_task overrides. Set the container's stopTimeout to 120 (the Fargate
maximum) so the agent has time to upload a temp snapshot on SIGTERM.

Tasks without a public IP need a NAT gateway for the agent's outbound
connections; with AWS_FARGATE_ASSIGN_PUBLIC_IP the default tunnel mode works
from public subnets without one.
"""
import asyncio

import boto3
from botocore.exceptions import ClientError
from pydantic_settings import SettingsConfigDict

from .base import (
    MINECRAFT_PORT,
    ConnectMode,
    Instance,
    InstanceState,
    InstanceStatus,
    LaunchSpec,
    Provider,
    ProviderError,
    ProviderSettings,
    split_list,
)


class AWSFargateSettings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="AWS_FARGATE_", extra="ignore")

    REGION: str = ""
    ACCESS_KEY_ID: str = ""          # empty: default boto3 credential chain
    SECRET_ACCESS_KEY: str = ""
    CLUSTER: str = ""
    TASK_DEFINITION: str = ""        # family[:revision] or ARN
    CONTAINER_NAME: str = "minecraft"
    SUBNETS: str = ""                # comma separated
    SECURITY_GROUPS: str = ""
    ASSIGN_PUBLIC_IP: bool = True
    CAPACITY_PROVIDER: str = ""      # e.g. FARGATE_SPOT; empty = launchType FARGATE
    EPHEMERAL_STORAGE_GIB: int = 0   # 21..200, 0 = the 20 GiB default
    CPU: float = 1                   # must be a valid Fargate cpu/memory pair
    MEMORY_MB: int = 4096


STATUS = {
    "PROVISIONING": InstanceStatus.PENDING,
    "PENDING": InstanceStatus.PENDING,
    "ACTIVATING": InstanceStatus.PENDING,
    "RUNNING": InstanceStatus.RUNNING,
}


class AWSFargateProvider(Provider):
    name = "awsfargate"
    settings_class = AWSFargateSettings
    default_connect_mode = ConnectMode.TUNNEL

    def __init__(self, settings=None):
        super().__init__(settings)
        s = self.settings
        if not (s.CLUSTER and s.TASK_DEFINITION and s.SUBNETS):
            raise ProviderError("AWS_FARGATE_CLUSTER, AWS_FARGATE_TASK_DEFINITION and AWS_FARGATE_SUBNETS are required")
        session = boto3.session.Session(
            aws_access_key_id=s.ACCESS_KEY_ID or None,
            aws_secret_access_key=s.SECRET_ACCESS_KEY or None,
            region_name=s.REGION or None,
        )
        self.ecs = session.client("ecs")
        self.ec2 = session.client("ec2")

    def _run(self, spec: LaunchSpec) -> Instance:
        s = self.settings
        overrides = {
            "containerOverrides": [{
                "name": s.CONTAINER_NAME,
                "environment": [{"name": k, "value": str(v)} for k, v in spec.env.items()],
            }],
            "cpu": str(int((spec.cpu or s.CPU) * 1024)),
            "memory": str(spec.memory_mb or s.MEMORY_MB),
        }
        if s.EPHEMERAL_STORAGE_GIB:
            overrides["ephemeralStorage"] = {"sizeInGiB": s.EPHEMERAL_STORAGE_GIB}
        kwargs = {}
        if s.CAPACITY_PROVIDER:
            kwargs["capacityProviderStrategy"] = [{"capacityProvider": s.CAPACITY_PROVIDER, "weight": 1}]
        else:
            kwargs["launchType"] = "FARGATE"
        network = {"subnets": split_list(s.SUBNETS), "assignPublicIp": "ENABLED" if s.ASSIGN_PUBLIC_IP else "DISABLED"}
        if s.SECURITY_GROUPS:
            network["securityGroups"] = split_list(s.SECURITY_GROUPS)
        response = self.ecs.run_task(
            cluster=s.CLUSTER,
            taskDefinition=s.TASK_DEFINITION,
            count=1,
            networkConfiguration={"awsvpcConfiguration": network},
            overrides=overrides,
            startedBy=f"realms-world-{spec.world_id}"[:36],
            tags=[{"key": k, "value": str(v)} for k, v in spec.labels.items()],
            **kwargs,
        )
        if response.get("failures") or not response.get("tasks"):
            raise ProviderError(f"ECS run_task failed: {response.get('failures')}")
        return Instance(instance_id=response["tasks"][0]["taskArn"], ports=self._ports())

    def _ports(self) -> dict:
        return {"private": MINECRAFT_PORT, "public": MINECRAFT_PORT if self.settings.ASSIGN_PUBLIC_IP else None}

    async def launch(self, spec: LaunchSpec) -> Instance:
        try:
            return await asyncio.to_thread(self._run, spec)
        except ClientError as e:
            raise ProviderError(f"ECS: {e}") from e

    def _public_ip(self, eni_id):
        if not eni_id or self.connect_mode != ConnectMode.PUBLIC:
            return None
        interfaces = self.ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])["NetworkInterfaces"]
        return (interfaces[0].get("Association") or {}).get("PublicIp") if interfaces else None

    def _inspect(self, instance_id: str) -> InstanceState:
        response = self.ecs.describe_tasks(cluster=self.settings.CLUSTER, tasks=[instance_id])
        # Stopped tasks stay visible for about an hour, then become failures.
        if not response.get("tasks"):
            return InstanceState(InstanceStatus.MISSING)
        task = response["tasks"][0]
        last = task.get("lastStatus", "")
        status = STATUS.get(last, InstanceStatus.STOPPED)
        if task.get("desiredStatus") == "STOPPED":
            status = InstanceStatus.STOPPED
        details = {}
        for attachment in task.get("attachments") or []:
            if attachment.get("type") == "ElasticNetworkInterface":
                details = {d["name"]: d["value"] for d in attachment.get("details") or []}
        ips = {"private": details.get("privateIPv4Address"), "public": None}
        if status == InstanceStatus.RUNNING:
            ips["public"] = self._public_ip(details.get("networkInterfaceId"))
        return InstanceState(status, ips=ips, ports=self._ports(), detail=task.get("stoppedReason") or last)

    async def inspect(self, instance_id: str) -> InstanceState:
        try:
            return await asyncio.to_thread(self._inspect, instance_id)
        except ClientError as e:
            raise ProviderError(f"ECS: {e}") from e

    def _stop(self, instance_id: str) -> None:
        try:
            self.ecs.stop_task(cluster=self.settings.CLUSTER, task=instance_id, reason="RealmsOrchestrator stop")
        except ClientError as e:
            # Already stopped or expired.
            if e.response.get("Error", {}).get("Code") not in ("InvalidParameterException", "ResourceNotFoundException"):
                raise

    async def destroy(self, instance_id: str) -> None:
        try:
            await asyncio.to_thread(self._stop, instance_id)
        except ClientError as e:
            raise ProviderError(f"ECS: {e}") from e
