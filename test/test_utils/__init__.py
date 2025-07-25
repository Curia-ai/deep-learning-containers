import json
import logging
import os
import re
import subprocess
import sys
import time
import pprint

from enum import Enum

import boto3
import requests

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from glob import glob
from invoke import run
from invoke.context import Context
from packaging.version import InvalidVersion, Version, parse
from packaging.specifiers import SpecifierSet
from datetime import date, datetime, timedelta
from retrying import retry
from pathlib import Path
import dataclasses
import uuid

# from security import EnhancedJSONEncoder

from src import config

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
LOGGER.addHandler(logging.StreamHandler(sys.stderr))

# Constant to represent default region for boto3 commands
DEFAULT_REGION = "us-west-2"
# Constant to represent region where p4de tests can be run
P4DE_REGION = "us-east-1"


def get_ami_id_boto3(region_name, ami_name_pattern, IncludeDeprecated=False):
    """
    For a given region and ami name pattern, return the latest ami-id
    """
    # Use max_attempts=10 because this function is used in global context, and all test jobs
    # get AMI IDs for tests regardless of whether they are used in that job.
    ec2_client = boto3.client(
        "ec2",
        region_name=region_name,
        config=Config(retries={"max_attempts": 10, "mode": "standard"}),
    )
    ami_list = ec2_client.describe_images(
        Filters=[{"Name": "name", "Values": [ami_name_pattern]}],
        Owners=["amazon"],
        IncludeDeprecated=IncludeDeprecated,
    )
    if not ami_list["Images"]:
        raise RuntimeError(f"Unable to find AMI with pattern {ami_name_pattern}")

    # NOTE: Hotfix for fetching latest DLAMI before certain creation date.
    # replace `ami_list["Images"]` with `filtered_images` in max() if needed.
    # filtered_images = [
    #     element
    #     for element in ami_list["Images"]
    #     if datetime.strptime(element["CreationDate"], "%Y-%m-%dT%H:%M:%S.%fZ")
    #     < datetime.strptime("2024-05-02", "%Y-%m-%d")
    # ]

    ami = max(ami_list["Images"], key=lambda x: x["CreationDate"])
    return ami["ImageId"]


def get_ami_id_ssm(region_name, parameter_path):
    """
    For a given region and parameter path, return the latest ami-id
    """
    # Use max_attempts=10 because this function is used in global context, and all test jobs
    # get AMI IDs for tests regardless of whether they are used in that job.
    ssm_client = boto3.client(
        "ssm",
        region_name=region_name,
        config=Config(retries={"max_attempts": 10, "mode": "standard"}),
    )
    ami = ssm_client.get_parameter(Name=parameter_path)

    # Special case for NVIDIA driver AMI paths
    if "base-oss-nvidia-driver-gpu-amazon-linux-2023" in parameter_path:
        ami_id = ami["Parameter"]["Value"]
    else:
        ami_id = eval(ami["Parameter"]["Value"])["image_id"]

    return ami_id


# DLAMI Base is split between OSS Nvidia Driver and Propietary Nvidia Driver. see https://docs.aws.amazon.com/dlami/latest/devguide/important-changes.html
UBUNTU_20_BASE_OSS_DLAMI_US_WEST_2 = get_ami_id_boto3(
    region_name="us-west-2",
    ami_name_pattern="Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 20.04) ????????",
)
UBUNTU_20_BASE_OSS_DLAMI_US_EAST_1 = get_ami_id_boto3(
    region_name="us-east-1",
    ami_name_pattern="Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 20.04) ????????",
)
UBUNTU_20_BASE_PROPRIETARY_DLAMI_US_WEST_2 = get_ami_id_boto3(
    region_name="us-west-2",
    ami_name_pattern="Deep Learning Base Proprietary Nvidia Driver GPU AMI (Ubuntu 20.04) ????????",
)
UBUNTU_20_BASE_PROPRIETARY_DLAMI_US_EAST_1 = get_ami_id_boto3(
    region_name="us-east-1",
    ami_name_pattern="Deep Learning Base Proprietary Nvidia Driver GPU AMI (Ubuntu 20.04) ????????",
)
AL2023_BASE_DLAMI_US_WEST_2 = get_ami_id_ssm(
    region_name="us-west-2",
    parameter_path="/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id",
)
AL2023_BASE_DLAMI_US_EAST_1 = get_ami_id_ssm(
    region_name="us-east-1",
    parameter_path="/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id",
)
AL2023_BASE_DLAMI_ARM64_US_WEST_2 = get_ami_id_ssm(
    region_name="us-west-2",
    parameter_path="/aws/service/deeplearning/ami/arm64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id ",
)
AL2023_BASE_DLAMI_ARM64_US_EAST_1 = get_ami_id_ssm(
    region_name="us-east-1",
    parameter_path="/aws/service/deeplearning/ami/arm64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id ",
)
AML2_BASE_ARM64_DLAMI_US_WEST_2 = get_ami_id_boto3(
    region_name="us-west-2",
    ami_name_pattern="Deep Learning ARM64 Base OSS Nvidia Driver GPU AMI (Amazon Linux 2) ????????",
)

# Using latest ARM64 AMI (pytorch) - however, this will fail for TF benchmarks, so TF benchmarks are currently
# disabled for Graviton.
UL20_BENCHMARK_CPU_ARM64_US_WEST_2 = get_ami_id_boto3(
    region_name="us-west-2",
    ami_name_pattern="Deep Learning ARM64 AMI OSS Nvidia Driver GPU PyTorch 2.2.? (Ubuntu 20.04) ????????",
    IncludeDeprecated=True,
)
AML2_CPU_ARM64_US_EAST_1 = get_ami_id_boto3(
    region_name="us-east-1",
    ami_name_pattern="Deep Learning Base AMI (Amazon Linux 2) Version ??.?",
)
PT_GPU_PY3_BENCHMARK_IMAGENET_AMI_US_EAST_1 = "ami-0673bb31cc62485dd"
PT_GPU_PY3_BENCHMARK_IMAGENET_AMI_US_WEST_2 = "ami-02d9a47bc61a31d43"

UL22_BASE_NEURON_US_WEST_2 = get_ami_id_boto3(
    region_name="us-west-2",
    ami_name_pattern="Deep Learning Base Neuron AMI (Ubuntu 22.04) ????????",
)

# Since NEURON TRN1 DLAMI is not released yet use a custom AMI
NEURON_INF1_AMI_US_WEST_2 = "ami-06a5a60d3801a57b7"
# Habana Base v0.15.4 ami
# UBUNTU_18_HPU_DLAMI_US_WEST_2 = "ami-0f051d0c1a667a106"
# UBUNTU_18_HPU_DLAMI_US_EAST_1 = "ami-04c47cb3d4fdaa874"
# Habana Base v1.2 ami
# UBUNTU_18_HPU_DLAMI_US_WEST_2 = "ami-047fd74c001116366"
# UBUNTU_18_HPU_DLAMI_US_EAST_1 = "ami-04c47cb3d4fdaa874"
# Habana Base v1.3 ami
# UBUNTU_18_HPU_DLAMI_US_WEST_2 = "ami-0ef18b1906e7010fb"
# UBUNTU_18_HPU_DLAMI_US_EAST_1 = "ami-040ef14d634e727a2"
# Habana Base v1.4.1 ami
# UBUNTU_18_HPU_DLAMI_US_WEST_2 = "ami-08e564663ef2e761c"
# UBUNTU_18_HPU_DLAMI_US_EAST_1 = "ami-06a0a1e2c90bfc1c8"
# Habana Base v1.5 ami
# UBUNTU_18_HPU_DLAMI_US_WEST_2 = "ami-06bb08c4a3c5ba3bb"
# UBUNTU_18_HPU_DLAMI_US_EAST_1 = "ami-009bbfadb94835957"
# Habana Base v1.6 ami
UBUNTU_18_HPU_DLAMI_US_WEST_2 = "ami-03cdcfc91a96a8f92"
UBUNTU_18_HPU_DLAMI_US_EAST_1 = "ami-0d83d7487f322545a"
UL_AMI_LIST = [
    UBUNTU_20_BASE_OSS_DLAMI_US_WEST_2,
    UBUNTU_20_BASE_OSS_DLAMI_US_EAST_1,
    UBUNTU_20_BASE_PROPRIETARY_DLAMI_US_WEST_2,
    UBUNTU_20_BASE_PROPRIETARY_DLAMI_US_EAST_1,
    UBUNTU_18_HPU_DLAMI_US_WEST_2,
    UBUNTU_18_HPU_DLAMI_US_EAST_1,
    PT_GPU_PY3_BENCHMARK_IMAGENET_AMI_US_EAST_1,
    PT_GPU_PY3_BENCHMARK_IMAGENET_AMI_US_WEST_2,
    UL22_BASE_NEURON_US_WEST_2,
    NEURON_INF1_AMI_US_WEST_2,
    UL20_BENCHMARK_CPU_ARM64_US_WEST_2,
]

# ECS images are maintained here: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-optimized_AMI.html
ECS_AML2_GPU_USWEST2 = get_ami_id_ssm(
    region_name="us-west-2",
    parameter_path="/aws/service/ecs/optimized-ami/amazon-linux-2/gpu/recommended",
)
ECS_AML2_CPU_USWEST2 = get_ami_id_ssm(
    region_name="us-west-2",
    parameter_path="/aws/service/ecs/optimized-ami/amazon-linux-2/recommended",
)
ECS_AML2_NEURON_USWEST2 = get_ami_id_ssm(
    region_name="us-west-2",
    parameter_path="/aws/service/ecs/optimized-ami/amazon-linux-2/inf/recommended",
)
ECS_AML2_ARM64_CPU_USWEST2 = get_ami_id_ssm(
    region_name="us-west-2",
    parameter_path="/aws/service/ecs/optimized-ami/amazon-linux-2/arm64/recommended",
)
NEURON_AL2_DLAMI = get_ami_id_boto3(
    region_name="us-west-2", ami_name_pattern="Deep Learning AMI (Amazon Linux 2) Version ??.?"
)

# Account ID of test executor
ACCOUNT_ID = boto3.client("sts", region_name=DEFAULT_REGION).get_caller_identity().get("Account")

# S3 bucket for TensorFlow models
TENSORFLOW_MODELS_BUCKET = "s3://tensoflow-trained-models"

# Used for referencing tests scripts from container_tests directory (i.e. from ECS cluster)
CONTAINER_TESTS_PREFIX = os.path.join(os.sep, "test", "bin")

# S3 Bucket to use to transfer tests into an EC2 instance
TEST_TRANSFER_S3_BUCKET = f"s3://dlinfra-tests-transfer-bucket-{ACCOUNT_ID}"

# S3 Transfer bucket region
TEST_TRANSFER_S3_BUCKET_REGION = "us-west-2"

# S3 Bucket to use to record benchmark results for further retrieving
BENCHMARK_RESULTS_S3_BUCKET = "s3://dlinfra-dlc-cicd-performance"

AL2023_HOME_DIR = "/home/ec2-user"

# Reason string for skipping tests in PR context
SKIP_PR_REASON = "Skipping test in PR context to speed up iteration time. Test will be run in nightly/release pipeline."

# Reason string for skipping tests in non-PR context
PR_ONLY_REASON = "Skipping test that doesn't need to be run outside of PR context."

KEYS_TO_DESTROY_FILE = os.path.join(os.sep, "tmp", "keys_to_destroy.txt")

# Sagemaker test types
SAGEMAKER_LOCAL_TEST_TYPE = "local"
SAGEMAKER_REMOTE_TEST_TYPE = "sagemaker"

PUBLIC_DLC_REGISTRY = "763104351884"
DLC_PUBLIC_REGISTRY_ALIAS = "public.ecr.aws/deep-learning-containers"

SAGEMAKER_EXECUTION_REGIONS = ["us-west-2", "us-east-1", "eu-west-1"]
# Before SM GA with Trn1, they support launch of ml.trn1 instance only in us-east-1. After SM GA this can be removed
SAGEMAKER_NEURON_EXECUTION_REGIONS = ["us-west-2"]
SAGEMAKER_NEURONX_EXECUTION_REGIONS = ["us-east-1"]

UPGRADE_ECR_REPO_NAME = "upgraded-image-ecr-scan-repo"
ECR_SCAN_HELPER_BUCKET = f"ecr-scan-helper-{ACCOUNT_ID}"
ECR_SCAN_FAILURE_ROUTINE_LAMBDA = "ecr-scan-failure-routine-lambda"

## Note that the region for the repo used for conducting ecr enhanced scans should be different from other
## repos since ecr enhanced scanning is activated in all the repos of a region and does not allow one to
## conduct basic scanning on some repos whereas enhanced scanning on others within the same region.
ECR_ENHANCED_SCANNING_REPO_NAME = "ecr-enhanced-scanning-dlc-repo"
ECR_ENHANCED_REPO_REGION = "us-west-1"

# region mapping for telemetry tests, need to be updated when new regions are added
TELEMETRY_REGION_MAPPING = {
    "ap-northeast-1": "ddce303c",
    "ap-northeast-2": "528c8d92",
    "ap-southeast-1": "c35f9f00",
    "ap-southeast-2": "d2add9c0",
    "ap-south-1": "9deb4123",
    "ca-central-1": "b95e2bf4",
    "eu-central-1": "bfec3957",
    "eu-north-1": "b453c092",
    "eu-west-1": "d763c260",
    "eu-west-2": "ea20d193",
    "eu-west-3": "1894043c",
    "sa-east-1": "030b4357",
    "us-east-1": "487d6534",
    "us-east-2": "72252b46",
    "us-west-1": "d02c1125",
    "us-west-2": "d8c0d063",
    "af-south-1": "08ea8dc5",
    "eu-south-1": "29566eac",
    "me-south-1": "7ea07793",
    "ap-southeast-7": "1699f14f",
    "ap-southeast-3": "be0a3174",
    "me-central-1": "6e06aaeb",
    "ap-east-1": "5e1fbf92",
    "ap-south-2": "50209442",
    "ap-northeast-3": "fa298003",
    "ap-southeast-5": "5852cd87",
    "us-northeast-1": "bbf9e961",
    "ap-southeast-4": "dc6f76ce",
    "mx-central-1": "ed0da79c",
    "il-central-1": "2fb2448e",
    "ap-east-2": "8947749e",
    "ca-west-1": "ea83ea06",
    "eu-south-2": "df2c9d70",
    "eu-central-2": "aa7aabcc",
}


class NightlyFeatureLabel(Enum):
    AWS_FRAMEWORK_INSTALLED = "aws_framework_installed"
    AWS_SMDEBUG_INSTALLED = "aws_smdebug_installed"
    AWS_SMDDP_INSTALLED = "aws_smddp_installed"
    AWS_SMMP_INSTALLED = "aws_smmp_installed"
    PYTORCH_INSTALLED = "pytorch_installed"
    AWS_S3_PLUGIN_INSTALLED = "aws_s3_plugin_installed"
    TORCHAUDIO_INSTALLED = "torchaudio_installed"
    TORCHVISION_INSTALLED = "torchvision_installed"
    TORCHDATA_INSTALLED = "torchdata_installed"


class MissingPythonVersionException(Exception):
    """
    When the Python Version is missing from an image_uri where it is expected to exist
    """

    pass


class CudaVersionTagNotFoundException(Exception):
    """
    When none of the tags of a GPU image have a Cuda version in them
    """

    pass


class DockerImagePullException(Exception):
    """
    When a docker image could not be pulled from ECR
    """

    pass


class SerialTestCaseExecutorException(Exception):
    """
    Raise for execute_serial_test_cases function
    """

    pass


class EnhancedJSONEncoder(json.JSONEncoder):
    """
    EnhancedJSONEncoder is required to dump dataclass objects as JSON.
    """

    def default(self, o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


def execute_serial_test_cases(test_cases, test_description="test"):
    """
    Helper function to execute tests in serial

    Args:
        test_cases (List): list of test cases, formatted as [(test_fn, (fn_arg1, fn_arg2 ..., fn_argN))]
        test_description (str, optional): Describe test for custom error message. Defaults to "test".
        bins (int, optional): If interested in optimizing the test across bins, use this feature
    """
    exceptions = []
    logging_stack = []
    times = {}
    for fn, args in test_cases:
        log_stack = []
        fn_name = fn.__name__
        start_time = datetime.now()
        log_stack.append(f"*********\nStarting {fn_name} at {start_time}\n")
        try:
            fn(*args)
            end_time = datetime.now()
            log_stack.append(f"\nEnding {fn_name} at {end_time}\n")
        except Exception as e:
            exceptions.append(f"{fn_name} FAILED WITH {type(e).__name__}:\n{e}")
            end_time = datetime.now()
            log_stack.append(f"\nFailing {fn_name} at {end_time}\n")
        finally:
            log_stack.append(
                f"Total execution time for {fn_name} {end_time - start_time}\n*********"
            )
            times[fn_name] = end_time - start_time
            logging_stack.append(log_stack)

    # Save logging to the end, as there may be other conccurent jobs
    for log_case in logging_stack:
        for line in log_case:
            LOGGER.info(line)

    pretty_times = pprint.pformat(times)
    LOGGER.info(pretty_times)

    if exceptions:
        raise SerialTestCaseExecutorException(
            f"Found {len(exceptions)} errors in {test_description}\n" + "\n\n".join(exceptions)
        )


def get_dockerfile_path_for_image(image_uri, python_version=None):
    """
    For a given image_uri, find the path within the repository to its corresponding dockerfile

    :param image_uri: str Image URI
    :return: str Absolute path to dockerfile
    """
    github_repo_path = os.path.abspath(os.path.curdir).split("test", 1)[0]

    framework, framework_version = get_framework_and_version_from_tag(image_uri)

    if "trcomp" in framework:
        # Replace the trcomp string as it is extracted from ECR repo name
        framework = framework.replace("_trcomp", "")
        framework_path = framework.replace("_", os.path.sep)
    elif "huggingface" in framework:
        framework_path = framework.replace("_", os.path.sep)
    elif "habana" in image_uri:
        framework_path = os.path.join("habana", framework)
    elif "stabilityai" in framework:
        framework_path = framework.replace("_", os.path.sep)
    else:
        framework_path = framework

    job_type = get_job_type_from_image(image_uri)

    short_framework_version = re.search(r"(\d+\.\d+)", image_uri).group(1)

    framework_version_path = os.path.join(
        github_repo_path, framework_path, job_type, "docker", short_framework_version
    )
    if not os.path.isdir(framework_version_path):
        long_framework_version = re.search(r"\d+(\.\d+){2}", image_uri).group()
        framework_version_path = os.path.join(
            github_repo_path, framework_path, job_type, "docker", long_framework_version
        )
    # While using the released images, they do not have python version at times
    # Hence, we want to allow a parameter that can pass the Python version externally in case it is not in the tag.
    if not python_version:
        python_version = re.search(r"py\d+", image_uri).group()

    python_version_path = os.path.join(framework_version_path, python_version)
    if not os.path.isdir(python_version_path):
        python_version_path = os.path.join(framework_version_path, "py3")

    device_type = get_processor_from_image_uri(image_uri)
    cuda_version = get_cuda_version_from_tag(image_uri)
    synapseai_version = get_synapseai_version_from_tag(image_uri)
    neuron_sdk_version = get_neuron_sdk_version_from_tag(image_uri)

    dockerfile_name = get_expected_dockerfile_filename(device_type, image_uri)
    dockerfiles_list = [
        path
        for path in glob(os.path.join(python_version_path, "**", dockerfile_name), recursive=True)
        if "example" not in path
    ]

    if device_type in ["gpu", "hpu", "neuron", "neuronx"]:
        if len(dockerfiles_list) > 1:
            if device_type == "gpu" and not cuda_version:
                raise LookupError(
                    f"dockerfiles_list has more than one result, and needs cuda_version to be in image_uri to "
                    f"uniquely identify the right dockerfile:\n"
                    f"{dockerfiles_list}"
                )
            if device_type == "hpu" and not synapseai_version:
                raise LookupError(
                    f"dockerfiles_list has more than one result, and needs synapseai_version to be in image_uri to "
                    f"uniquely identify the right dockerfile:\n"
                    f"{dockerfiles_list}"
                )
            if "neuron" in device_type and not neuron_sdk_version:
                raise LookupError(
                    f"dockerfiles_list has more than one result, and needs neuron_sdk_version to be in image_uri to "
                    f"uniquely identify the right dockerfile:\n"
                    f"{dockerfiles_list}"
                )
        for dockerfile_path in dockerfiles_list:
            if cuda_version:
                if cuda_version in dockerfile_path:
                    return dockerfile_path
            elif synapseai_version:
                if synapseai_version in dockerfile_path:
                    return dockerfile_path
            elif neuron_sdk_version:
                if neuron_sdk_version in dockerfile_path:
                    return dockerfile_path
        raise LookupError(
            f"Failed to find a dockerfile path for {cuda_version} in:\n{dockerfiles_list}"
        )

    assert (
        len(dockerfiles_list) == 1
    ), f"No unique dockerfile path in:\n{dockerfiles_list}\nfor image: {image_uri}"

    return dockerfiles_list[0]


def get_expected_dockerfile_filename(device_type, image_uri):
    if is_covered_by_ec2_sm_split(image_uri):
        if "graviton" in image_uri:
            return f"Dockerfile.graviton.{device_type}"
        elif "arm64" in image_uri:
            return f"Dockerfile.arm64.{device_type}"
        elif is_ec2_sm_in_same_dockerfile(image_uri):
            if "pytorch-trcomp-training" in image_uri:
                return f"Dockerfile.trcomp.{device_type}"
            else:
                return f"Dockerfile.{device_type}"
        elif is_ec2_image(image_uri):
            return f"Dockerfile.ec2.{device_type}"
        else:
            return f"Dockerfile.sagemaker.{device_type}"

    ## TODO: Keeping here for backward compatibility, should be removed in future when the
    ## functions is_covered_by_ec2_sm_split and is_ec2_sm_in_same_dockerfile are made exhaustive
    if is_ec2_image(image_uri):
        return f"Dockerfile.ec2.{device_type}"
    if is_sagemaker_image(image_uri):
        return f"Dockerfile.sagemaker.{device_type}"
    if is_trcomp_image(image_uri):
        return f"Dockerfile.trcomp.{device_type}"
    return f"Dockerfile.{device_type}"


def get_canary_helper_bucket_name():
    bucket_name = os.getenv("CANARY_HELPER_BUCKET")
    assert bucket_name, "Unable to find bucket name in CANARY_HELPER_BUCKET env variable"
    return bucket_name


def get_customer_type():
    return os.getenv("CUSTOMER_TYPE")


def get_image_type():
    """
    Env variable should return training or inference
    """
    return os.getenv("IMAGE_TYPE")


def get_test_job_arch_type():
    """
    Env variable should return graviton, arm64, x86, or None
    """
    return os.getenv("ARCH_TYPE", "x86")


def get_ecr_repo_name(image_uri):
    """
    Retrieve ECR repository name from image URI
    :param image_uri: str ECR Image URI
    :return: str ECR repository name
    """
    ecr_repo_name = image_uri.split("/")[-1].split(":")[0]
    return ecr_repo_name


def is_tf_version(required_version, image_uri):
    """
    Validate that image_uri has framework version equal to required_version
    Relying on current convention to include TF version into an image tag for all
    TF based frameworks

    :param required_version: str Framework version which is required from the image_uri
    :param image_uri: str ECR Image URI for the image to be validated
    :return: bool True if image_uri has same framework version as required_version, else False
    """
    image_framework_name, image_framework_version = get_framework_and_version_from_tag(image_uri)
    required_version_specifier_set = SpecifierSet(f"=={required_version}.*")
    return (
        is_tf_based_framework(image_framework_name)
        and image_framework_version in required_version_specifier_set
    )


def is_tf_based_framework(name):
    """
    Checks whether framework is TF based.
    Relying on current convention to include "tensorflow" into TF based names
    E.g. "huggingface-tensorflow" or "huggingface-tensorflow-trcomp"
    """
    return "tensorflow" in name


def is_equal_to_framework_version(version_required, image_uri, framework):
    """
    Validate that image_uri has framework version exactly equal to version_required

    :param version_required: str Framework version that image_uri is required to be at
    :param image_uri: str ECR Image URI for the image to be validated
    :param framework: str Framework installed in image
    :return: bool True if image_uri has framework version equal to version_required, else False
    """
    image_framework_name, image_framework_version = get_framework_and_version_from_tag(image_uri)
    return image_framework_name == framework and Version(image_framework_version) in SpecifierSet(
        f"=={version_required}"
    )


def is_above_framework_version(version_lower_bound, image_uri, framework):
    """
    Validate that image_uri has framework version strictly less than version_upper_bound

    :param version_lower_bound: str Framework version that image_uri is required to be above
    :param image_uri: str ECR Image URI for the image to be validated
    :param framework: str Framework installed in image
    :return: bool True if image_uri has framework version more than version_lower_bound, else False
    """
    image_framework_name, image_framework_version = get_framework_and_version_from_tag(image_uri)
    required_version_specifier_set = SpecifierSet(f">{version_lower_bound}")
    return (
        image_framework_name == framework
        and image_framework_version in required_version_specifier_set
    )


def is_below_framework_version(version_upper_bound, image_uri, framework):
    """
    Validate that image_uri has framework version strictly less than version_upper_bound

    :param version_upper_bound: str Framework version that image_uri is required to be below
    :param image_uri: str ECR Image URI for the image to be validated
    :return: bool True if image_uri has framework version less than version_upper_bound, else False
    """
    image_framework_name, image_framework_version = get_framework_and_version_from_tag(image_uri)
    required_version_specifier_set = SpecifierSet(f"<{version_upper_bound}")
    return (
        image_framework_name == framework
        and image_framework_version in required_version_specifier_set
    )


def is_below_cuda_version(version_upper_bound, image_uri):
    """
    Validate that image_uri has cuda version strictly less than version_upper_bound

    :param version_upper_bound: str Cuda version that image_uri is required to be below
    :param image_uri: str ECR Image URI for the image to be validated
    :return: bool True if image_uri has cuda version less than version_upper_bound, else False
    """
    cuda_version = get_cuda_version_from_tag(image_uri)
    numbers = cuda_version[2:]
    numeric_version = f"{numbers[:-1]}.{numbers[-1]}"
    required_version_specifier_set = SpecifierSet(f"<{version_upper_bound}")
    return numeric_version in required_version_specifier_set


def is_image_incompatible_with_instance_type(image_uri, ec2_instance_type):
    """
    Check for all compatibility issues between DLC Image Types and EC2 Instance Types.
    Currently configured to fail on the following checks:
        1. p4d.24xlarge instance type is used with a cuda<11.0 image
        2. g5.8xlarge instance type is used with a cuda=11.0 image for MXNET framework

    :param image_uri: ECR Image URI in valid DLC-format
    :param ec2_instance_type: EC2 Instance Type
    :return: bool True if there are incompatibilities, False if there aren't
    """
    incompatible_conditions = []
    framework, framework_version = get_framework_and_version_from_tag(image_uri)

    image_is_cuda10_on_incompatible_p4d_instance = (
        get_processor_from_image_uri(image_uri) == "gpu"
        and get_cuda_version_from_tag(image_uri).startswith("cu10")
        and ec2_instance_type in ["p4d.24xlarge"]
    )
    incompatible_conditions.append(image_is_cuda10_on_incompatible_p4d_instance)

    image_is_cuda11_on_incompatible_p2_instance_mxnet = (
        framework == "mxnet"
        and get_processor_from_image_uri(image_uri) == "gpu"
        and get_cuda_version_from_tag(image_uri).startswith("cu11")
        and ec2_instance_type in ["g5.12xlarge"]
    )
    incompatible_conditions.append(image_is_cuda11_on_incompatible_p2_instance_mxnet)

    image_is_pytorch_1_11_on_incompatible_p2_instance_pytorch = (
        framework == "pytorch"
        and Version(framework_version) in SpecifierSet("==1.11.*")
        and get_processor_from_image_uri(image_uri) == "gpu"
        and ec2_instance_type in ["g5.12xlarge"]
    )
    incompatible_conditions.append(image_is_pytorch_1_11_on_incompatible_p2_instance_pytorch)

    return any(incompatible_conditions)


def is_image_incompatible_with_AL2023_for_gdrcopy(image_uri):
    """
    Images may contain gdrcopy versions that are older than the drivers running on the base AL2023 DLAMI, which could result in compatibility issues.
    """
    incompatible_conditions = []
    framework, framework_version = get_framework_and_version_from_tag(image_uri)

    image_is_pytorch_lower_than_or_equal_to_2_6 = framework == "pytorch" and Version(
        framework_version
    ) in SpecifierSet("<2.7.*")

    incompatible_conditions.append(image_is_pytorch_lower_than_or_equal_to_2_6)

    return any(incompatible_conditions)


def get_repository_local_path():
    git_repo_path = os.getcwd().split("/test/")[0]
    return git_repo_path


def get_inference_server_type(image_uri):
    if "pytorch" not in image_uri:
        return "mms"
    if "neuron" in image_uri:
        return "ts"
    image_tag = image_uri.split(":")[1]
    # recent changes to the packaging package
    # updated parse function to return Version type
    # and deprecated LegacyVersion
    # attempt to parse pytorch version would raise an InvalidVersion exception
    # return that as "mms"
    try:
        pytorch_ver = parse(image_tag.split("-")[0])
        if pytorch_ver < Version("1.6"):
            return "mms"
    except InvalidVersion as e:
        return "mms"
    return "ts"


def get_build_context():
    return os.getenv("BUILD_CONTEXT")


def is_pr_context():
    return os.getenv("BUILD_CONTEXT") == "PR"


def is_canary_context():
    return os.getenv("BUILD_CONTEXT") == "CANARY"


def is_mainline_context():
    return os.getenv("BUILD_CONTEXT") == "MAINLINE"


def is_deep_canary_context():
    return os.getenv("BUILD_CONTEXT") == "DEEP_CANARY" or (
        os.getenv("BUILD_CONTEXT") == "PR"
        and os.getenv("DEEP_CANARY_MODE", "false").lower() == "true"
    )


def is_nightly_context():
    return (
        os.getenv("BUILD_CONTEXT") == "NIGHTLY"
        or os.getenv("NIGHTLY_PR_TEST_MODE", "false").lower() == "true"
    )


def is_empty_build_context():
    return not os.getenv("BUILD_CONTEXT")


def is_graviton_architecture():
    return os.getenv("ARCH_TYPE") == "graviton"


def is_arm64_architecture():
    return os.getenv("ARCH_TYPE") == "arm64"


def is_dlc_cicd_context():
    return os.getenv("BUILD_CONTEXT") in ["PR", "CANARY", "NIGHTLY", "MAINLINE"]


def is_efa_dedicated():
    return os.getenv("EFA_DEDICATED", "False").lower() == "true"


def are_heavy_instance_ec2_tests_enabled():
    return os.getenv("HEAVY_INSTANCE_EC2_TESTS_ENABLED", "False").lower() == "true"


def is_generic_image():
    return os.getenv("IS_GENERIC_IMAGE", "false").lower() == "true"


def get_allowlist_path_for_enhanced_scan_from_env_variable():
    return os.getenv("ALLOWLIST_PATH_ENHSCAN")


def is_rc_test_context():
    return config.is_sm_rc_test_enabled()


def is_sanity_test_enabled():
    return config.is_sanity_test_enabled()


def is_security_test_enabled():
    return config.is_security_test_enabled()


def is_huggingface_image():
    if not os.getenv("FRAMEWORK_BUILDSPEC_FILE"):
        return False
    return os.getenv("FRAMEWORK_BUILDSPEC_FILE").startswith("huggingface")


def is_covered_by_ec2_sm_split(image_uri):
    ec2_sm_split_images = {
        "pytorch": SpecifierSet(">=1.10.0"),
        "tensorflow": SpecifierSet(">=2.7.0"),
        "pytorch_trcomp": SpecifierSet(">=1.12.0"),
        "mxnet": SpecifierSet(">=1.9.0"),
    }
    framework, version = get_framework_and_version_from_tag(image_uri)
    return framework in ec2_sm_split_images and Version(version) in ec2_sm_split_images[framework]


def is_ec2_sm_in_same_dockerfile(image_uri):
    same_sm_ec2_dockerfile_record = {
        "pytorch": SpecifierSet(">=1.11.0"),
        "tensorflow": SpecifierSet(">=2.8.0"),
        "pytorch_trcomp": SpecifierSet(">=1.12.0"),
        "mxnet": SpecifierSet(">=1.9.0"),
    }
    framework, version = get_framework_and_version_from_tag(image_uri)
    return (
        framework in same_sm_ec2_dockerfile_record
        and Version(version) in same_sm_ec2_dockerfile_record[framework]
    )


def is_ec2_image(image_uri):
    return "-ec2" in image_uri


def is_sagemaker_image(image_uri):
    return "-sagemaker" in image_uri


def is_trcomp_image(image_uri):
    return "-trcomp" in image_uri


def is_time_for_canary_safety_scan():
    """
    Canary tests run every 15 minutes.
    Using a 20 minutes interval to make tests run only once a day around 9 am PST (10 am during winter time).
    """
    current_utc_time = time.gmtime()
    return current_utc_time.tm_hour == 16 and (0 < current_utc_time.tm_min < 20)


def is_time_for_invoking_ecr_scan_failure_routine_lambda():
    """
    Canary tests run every 15 minutes.
    Using a 20 minutes interval to make tests run only once a day around 9 am PST (10 am during winter time).
    """
    current_utc_time = time.gmtime()
    return current_utc_time.tm_hour == 16 and (0 < current_utc_time.tm_min < 20)


def is_test_phase():
    return "TEST_TYPE" in os.environ


def _get_remote_override_flags():
    try:
        s3_client = boto3.client("s3")
        sts_client = boto3.client("sts")
        account_id = sts_client.get_caller_identity().get("Account")
        result = s3_client.get_object(
            Bucket=f"dlc-cicd-helper-{account_id}", Key="override_tests_flags.json"
        )
        json_content = json.loads(result["Body"].read().decode("utf-8"))
    except ClientError as e:
        LOGGER.warning("ClientError when performing S3/STS operation: {}".format(e))
        json_content = {}
    return json_content


# Now we can skip EFA tests on pipeline without making any source code change
def are_efa_tests_disabled():
    disable_efa_tests = (
        is_pr_context() and os.getenv("DISABLE_EFA_TESTS", "False").lower() == "true"
    )

    remote_override_flags = _get_remote_override_flags()
    override_disable_efa_tests = (
        remote_override_flags.get("disable_efa_tests", "false").lower() == "true"
    )

    return disable_efa_tests or override_disable_efa_tests


def is_safety_test_context():
    return config.is_safety_check_test_enabled()


def is_test_disabled(test_name, build_name, version):
    """
    Expected format of remote_override_flags:
    {
        "CB Project Name for Test Type A": {
            "CodeBuild Resolved Source Version": ["test_type_A_test_function_1", "test_type_A_test_function_2"]
        },
        "CB Project Name for Test Type B": {
            "CodeBuild Resolved Source Version": ["test_type_B_test_function_1", "test_type_B_test_function_2"]
        }
    }

    :param test_name: str Test Function node name (includes parametrized values in string)
    :param build_name: str Build Project name of current execution
    :param version: str Source Version of current execution
    :return: bool True if test is disabled as per remote override, False otherwise
    """
    remote_override_flags = _get_remote_override_flags()
    remote_override_build = remote_override_flags.get(build_name, {})
    if version in remote_override_build:
        return not remote_override_build[version] or any(
            [test_keyword in test_name for test_keyword in remote_override_build[version]]
        )
    return False


def run_subprocess_cmd(cmd, failure="Command failed"):
    import pytest

    command = subprocess.run(cmd, stdout=subprocess.PIPE, shell=True)
    if command.returncode:
        pytest.fail(f"{failure}. Error log:\n{command.stdout.decode()}")
    return command


def login_to_ecr_registry(context, account_id, region):
    """
    Function to log into an ecr registry

    :param context: either invoke context object or fabric connection object
    :param account_id: Account ID with the desired ecr registry
    :param region: i.e. us-west-2
    """
    context.run(
        f"aws ecr get-login-password --region {region} | docker login --username AWS "
        f"--password-stdin {account_id}.dkr.ecr.{region}.amazonaws.com"
    )


def retry_if_result_is_false(result):
    """Return True if we should retry (in this case retry if the result is False), False otherwise"""
    return result is False


@retry(
    stop_max_attempt_number=10,
    wait_fixed=10000,
    retry_on_result=retry_if_result_is_false,
)
def request_mxnet_inference(ip_address="127.0.0.1", port="80", connection=None, model="squeezenet"):
    """
    Send request to container to test inference on kitten.jpg
    :param ip_address:
    :param port:
    :connection: ec2_connection object to run the commands remotely over ssh
    :return: <bool> True/False based on result of inference
    """
    conn_run = connection.run if connection is not None else run

    # Check if image already exists
    run_out = conn_run("[ -f kitten.jpg ]", warn=True)
    if run_out.return_code != 0:
        conn_run("curl -O https://s3.amazonaws.com/model-server/inputs/kitten.jpg", hide=True)

    run_out = conn_run(
        f"curl -X POST http://{ip_address}:{port}/predictions/{model} -T kitten.jpg", warn=True
    )

    # The run_out.return_code is not reliable, since sometimes predict request may succeed but the returned result
    # is 404. Hence the extra check.
    if run_out.return_code != 0 or "probability" not in run_out.stdout:
        return False

    return True


@retry(stop_max_attempt_number=10, wait_fixed=10000, retry_on_result=retry_if_result_is_false)
def request_mxnet_inference_gluonnlp(ip_address="127.0.0.1", port="80", connection=None):
    """
    Send request to container to test inference for predicting sentiments.
    :param ip_address:
    :param port:
    :connection: ec2_connection object to run the commands remotely over ssh
    :return: <bool> True/False based on result of inference
    """
    conn_run = connection.run if connection is not None else run
    run_out = conn_run(
        (
            f"curl -X POST http://{ip_address}:{port}/predictions/bert_sst/predict -F "
            '\'data=["Positive sentiment", "Negative sentiment"]\''
        ),
        warn=True,
    )

    # The run_out.return_code is not reliable, since sometimes predict request may succeed but the returned result
    # is 404. Hence the extra check.
    if run_out.return_code != 0 or "1" not in run_out.stdout:
        return False

    return True


@retry(
    stop_max_attempt_number=10,
    wait_fixed=10000,
    retry_on_result=retry_if_result_is_false,
)
def request_pytorch_inference_densenet(
    ip_address="127.0.0.1",
    port="80",
    connection=None,
    model_name="pytorch-densenet",
    server_type="ts",
):
    """
    Send request to container to test inference on flower.jpg
    :param ip_address: str
    :param port: str
    :param connection: obj
    :param model_name: str
    :return: <bool> True/False based on result of inference
    """
    conn_run = connection.run if connection is not None else run
    # Check if image already exists
    run_out = conn_run("[ -f flower.jpg ]", warn=True)
    if run_out.return_code != 0:
        conn_run("curl -O https://s3.amazonaws.com/model-server/inputs/flower.jpg", hide=True)

    run_out = conn_run(
        f"curl -X POST http://{ip_address}:{port}/predictions/{model_name} -T flower.jpg",
        hide=True,
        warn=True,
    )

    # The run_out.return_code is not reliable, since sometimes predict request may succeed but the returned result
    # is 404. Hence the extra check.
    if run_out.return_code != 0:
        LOGGER.error(
            f"run_out.return_code is not reliable. Predict requests may succeed but return a 404 error instead.\nReturn Code: {run_out.return_code}\nError: {run_out.stderr}\nStdOut: {run_out.stdout}"
        )
        return False
    else:
        inference_output = json.loads(run_out.stdout.strip("\n"))
        LOGGER.info(f"Inference Output = {json.dumps(inference_output, indent=4)}")
        if not (
            (
                "neuron" in model_name
                and isinstance(inference_output, list)
                and len(inference_output) == 3
            )
            or (
                server_type == "ts"
                and isinstance(inference_output, dict)
                and len(inference_output) == 5
            )
            or (
                server_type == "mms"
                and isinstance(inference_output, list)
                and len(inference_output) == 5
            )
        ):
            return False

    return True


@retry(stop_max_attempt_number=20, wait_fixed=15000, retry_on_result=retry_if_result_is_false)
def request_tensorflow_inference(
    model_name,
    ip_address="127.0.0.1",
    port="8501",
    inference_string="'{\"instances\": [1.0, 2.0, 5.0]}'",
    connection=None,
):
    """
    Method to run tensorflow inference on half_plus_two model using CURL command
    :param model_name:
    :param ip_address:
    :param port:
    :connection: ec2_connection object to run the commands remotely over ssh
    :return:
    """
    conn_run = connection.run if connection is not None else run

    curl_command = f"curl -d {inference_string} -X POST  http://{ip_address}:{port}/v1/models/{model_name}:predict"
    LOGGER.info(f"Initiating curl command: {curl_command}")
    run_out = conn_run(curl_command, warn=True)
    LOGGER.info(f"Curl command completed with output: {run_out.stdout}")

    # The run_out.return_code is not reliable, since sometimes predict request may succeed but the returned result
    # is 404. Hence the extra check.
    if run_out.return_code != 0 or "predictions" not in run_out.stdout:
        return False

    return True


@retry(stop_max_attempt_number=20, wait_fixed=10000, retry_on_result=retry_if_result_is_false)
def request_tensorflow_inference_nlp(model_name, ip_address="127.0.0.1", port="8501"):
    """
    Method to run tensorflow inference on half_plus_two model using CURL command
    :param model_name:
    :param ip_address:
    :param port:
    :connection: ec2_connection object to run the commands remotely over ssh
    :return:
    """
    inference_string = "'{\"instances\": [[2,1952,25,10901,3]]}'"
    run_out = run(
        f"curl -d {inference_string} -X POST http://{ip_address}:{port}/v1/models/{model_name}:predict",
        warn=True,
    )

    # The run_out.return_code is not reliable, since sometimes predict request may succeed but the returned result
    # is 404. Hence the extra check.
    if run_out.return_code != 0 or "predictions" not in run_out.stdout:
        return False

    return True


def request_tensorflow_inference_grpc(
    script_file_path, ip_address="127.0.0.1", port="8500", connection=None
):
    """
    Method to run tensorflow inference on MNIST model using gRPC protocol
    :param script_file_path:
    :param ip_address:
    :param port:
    :param connection:
    :return:
    """
    conn_run = connection.run if connection is not None else run
    conn_run(f"python {script_file_path} --num_tests=1000 --server={ip_address}:{port}", hide=True)


def get_inference_run_command(image_uri, model_names, processor="cpu"):
    """
    Helper function to format run command for MMS
    :param image_uri:
    :param model_names:
    :param processor:
    :return: <str> Command to start MMS server with given model
    """
    server_type = get_inference_server_type(image_uri)
    if processor == "eia":
        multi_model_location = {
            "resnet-152-eia": "https://s3.amazonaws.com/model-server/model_archive_1.0/resnet-152-eia-1-7-0.mar",
            "resnet-152-eia-1-5-1": "https://s3.amazonaws.com/model-server/model_archive_1.0/resnet-152-eia-1-5-1.mar",
            "pytorch-densenet": "https://aws-dlc-sample-models.s3.amazonaws.com/pytorch/densenet_eia/densenet_eia_v1_5_1.mar",
            "pytorch-densenet-v1-3-1": "https://aws-dlc-sample-models.s3.amazonaws.com/pytorch/densenet_eia/densenet_eia_v1_3_1.mar",
        }
    elif server_type == "ts":
        multi_model_location = {
            "squeezenet": "https://torchserve.s3.amazonaws.com/mar_files/squeezenet1_1.mar",
            "pytorch-densenet": "https://torchserve.s3.amazonaws.com/mar_files/densenet161.mar",
            "pytorch-resnet-neuron": "https://aws-dlc-sample-models.s3.amazonaws.com/pytorch/Resnet50-neuron.mar",
            "pytorch-densenet-inductor": "https://aws-dlc-sample-models.s3.amazonaws.com/pytorch/densenet161-inductor.mar",
            "pytorch-resnet-neuronx": "https://aws-dlc-pt-sample-models.s3.amazonaws.com/resnet50/resnet_neuronx.mar",
        }
    else:
        multi_model_location = {
            "squeezenet": "https://s3.amazonaws.com/model-server/models/squeezenet_v1.1/squeezenet_v1.1.model",
            "pytorch-densenet": "https://dlc-samples.s3.amazonaws.com/pytorch/multi-model-server/densenet/densenet.mar",
            "bert_sst": "https://aws-dlc-sample-models.s3.amazonaws.com/bert_sst/bert_sst.mar",
            "mxnet-resnet-neuron": "https://aws-dlc-sample-models.s3.amazonaws.com/mxnet/Resnet50-neuron.mar",
        }

    if not isinstance(model_names, list):
        model_names = [model_names]

    for model_name in model_names:
        if model_name not in multi_model_location:
            raise Exception("No entry found for model {} in dictionary".format(model_name))

    parameters = ["{}={}".format(name, multi_model_location[name]) for name in model_names]

    if server_type == "ts":
        server_cmd = "torchserve"
    else:
        server_cmd = "multi-model-server"

    if processor != "neuron":
        # PyTorch 2.4 requires token authentication to be disabled.
        _framework, _version = get_framework_and_version_from_tag(image_uri=image_uri)
        auth_arg = (
            " --disable-token-auth"
            if _framework == "pytorch" and Version(_version) in SpecifierSet(">=2.4")
            else ""
        )
        mms_command = (
            f"{server_cmd} --start{auth_arg} --{server_type}-config /home/model-server/config.properties --models "
            + " ".join(parameters)
        )
    else:
        # Temp till the mxnet dockerfile also have the neuron entrypoint file
        if server_type == "ts":
            mms_command = (
                f"{server_cmd} --start --{server_type}-config /home/model-server/config.properties --models "
                + " ".join(parameters)
            )
        else:
            mms_command = (
                f"/usr/local/bin/entrypoint.sh -t /home/model-server/config.properties -m "
                + " ".join(parameters)
            )

    return mms_command


def get_tensorflow_model_name(processor, model_name):
    """
    Helper function to get tensorflow model name
    :param processor: Processor Type
    :param model_name: Name of model to be used
    :return: File name for model being used
    """
    tensorflow_models = {
        "saved_model_half_plus_two": {
            "cpu": "saved_model_half_plus_two_cpu",
            "gpu": "saved_model_half_plus_two_gpu",
            "eia": "saved_model_half_plus_two",
        },
        "albert": {
            "cpu": "albert",
            "gpu": "albert",
            "eia": "albert",
        },
        "saved_model_half_plus_three": {"eia": "saved_model_half_plus_three"},
        "simple": {"neuron": "simple", "neuronx": "simple_x"},
    }
    if model_name in tensorflow_models:
        return tensorflow_models[model_name][processor]
    else:
        raise Exception(f"No entry found for model {model_name} in dictionary")


def generate_ssh_keypair(ec2_client, key_name):
    pwd = run("pwd", hide=True).stdout.strip("\n")
    key_filename = os.path.join(pwd, f"{key_name}.pem")
    if os.path.exists(key_filename):
        run(f"chmod 400 {key_filename}")
        return key_filename
    try:
        key_pair = ec2_client.create_key_pair(KeyName=key_name)
    except ClientError as e:
        if "InvalidKeyPair.Duplicate" in f"{e}":
            # Wait 10 seconds for key to be created to avoid race condition
            time.sleep(10)
            if os.path.exists(key_filename):
                run(f"chmod 400 {key_filename}")
                return key_filename
        raise e

    run(f"echo '{key_pair['KeyMaterial']}' > {key_filename}")
    run(f"chmod 400 {key_filename}")
    return key_filename


def destroy_ssh_keypair(ec2_client, key_filename):
    key_name = os.path.basename(key_filename).split(".pem")[0]
    response = ec2_client.delete_key_pair(KeyName=key_name)
    run(f"rm -f {key_filename}")
    return response, key_name


def upload_tests_to_s3(testname_datetime_suffix):
    """
    Upload test-related artifacts to unique s3 location.
    Allows each test to have a unique remote location for test scripts and files.
    These uploaded files and folders are copied into a container running an ECS test.
    :param testname_datetime_suffix: test name and datetime suffix that is unique to a test
    :return: <bool> True if upload was successful, False if any failure during upload
    """
    s3_test_location = os.path.join(TEST_TRANSFER_S3_BUCKET, testname_datetime_suffix)
    run_out = run(f"aws s3 ls {s3_test_location}", warn=True)
    if run_out.return_code == 0:
        raise FileExistsError(
            f"{s3_test_location} already exists. Skipping upload and failing the test."
        )

    path = run("pwd", hide=True).stdout.strip("\n")
    if "dlc_tests" not in path:
        raise EnvironmentError("Test is being run from wrong path")
    while os.path.basename(path) != "dlc_tests":
        path = os.path.dirname(path)
    container_tests_path = os.path.join(path, "container_tests")

    run(f"aws s3 cp --recursive {container_tests_path}/ {s3_test_location}/")
    return s3_test_location


def delete_uploaded_tests_from_s3(s3_test_location):
    """
    Delete s3 bucket data related to current test after test is completed
    :param s3_test_location: S3 URI for test artifacts to be removed
    :return: <bool> True/False based on success/failure of removal
    """
    run(f"aws s3 rm --recursive {s3_test_location}")


def get_dlc_images():
    if is_deep_canary_context():
        deep_canary_images = get_deep_canary_images(
            canary_framework=os.getenv("FRAMEWORK"),
            canary_image_type=get_image_type(),
            canary_arch_type=get_test_job_arch_type(),
            canary_region=os.getenv("AWS_REGION"),
            canary_region_prod_account=os.getenv("REGIONAL_PROD_ACCOUNT", PUBLIC_DLC_REGISTRY),
            is_public_registry=os.getenv("IS_PUBLIC_REGISTRY_CANARY", "false").lower() == "true",
        )
        return " ".join(deep_canary_images)
    elif is_pr_context() or is_empty_build_context():
        return os.getenv("DLC_IMAGES")
    elif is_canary_context():
        # TODO: Remove 'training' default once training-specific canaries are added
        image_type = get_image_type() or "training"
        return parse_canary_images(os.getenv("FRAMEWORK"), os.getenv("AWS_REGION"), image_type)
    elif is_mainline_context():
        test_env_file = os.path.join(
            os.getenv("CODEBUILD_SRC_DIR_DLC_IMAGES_JSON"), "test_type_images.json"
        )
        with open(test_env_file) as test_env:
            test_images = json.load(test_env)
        for dlc_test_type, images in test_images.items():
            if "sanity" in dlc_test_type:
                return " ".join(images)
        raise RuntimeError(f"Cannot find any images for in {test_images}")
    return None


def get_deep_canary_images(
    canary_framework,
    canary_image_type,
    canary_arch_type,
    canary_region,
    canary_region_prod_account,
    is_public_registry=False,
):
    """
    For an input combination of canary job specs, find a matching list of image uris to be tested
    :param canary_framework: str Framework Name
    :param canary_image_type: str "training" or "inference"
    :param canary_arch_type: str "x86" or "graviton" or "arm64"
    :param canary_region: str Region Name
    :param canary_region_prod_account: str DLC Production Account ID in this region
    :return: list<str> List of image uris regionalized for canary_region
    """
    assert (
        canary_framework
        and canary_image_type
        and canary_arch_type
        and canary_region
        and canary_region_prod_account
    ), (
        "Incorrect spec for one or more of the following:\n"
        f"canary_framework = {canary_framework}\n"
        f"canary_image_type = {canary_image_type}\n"
        f"canary_arch_type = {canary_arch_type}\n"
        f"canary_region = {canary_region}\n"
        f"canary_region_prod_account = {canary_region_prod_account}"
    )
    all_images = get_canary_image_uris_from_bucket()
    matching_images = []
    for image_uri in all_images:
        image_framework = get_framework_from_image_uri(image_uri)
        image_type = get_image_type_from_tag(image_uri)
        image_arch_type = get_image_arch_type_from_tag(image_uri)
        image_region = get_region_from_image_uri(image_uri)
        image_account_id = get_account_id_from_image_uri(image_uri)
        if (
            canary_framework == image_framework
            and canary_image_type == image_type
            and canary_arch_type == image_arch_type
        ):
            if is_public_registry:
                # For public registry, we use the public account ID
                image_repository = image_uri.split("/")[-1].split(":")[0]
                image_tag = image_uri.split(":")[-1]
                regionalized_image_uri = (
                    f"{DLC_PUBLIC_REGISTRY_ALIAS}/{image_repository}:{image_tag}"
                )
            else:
                regionalized_image_uri = image_uri.replace(image_region, canary_region).replace(
                    image_account_id, canary_region_prod_account
                )
            matching_images.append(regionalized_image_uri)
    return matching_images


def get_canary_image_uris_from_bucket():
    """
    Helper function to get canary-tested DLC Image URIs

    :return: list of [str<DLC Image URI>]
    """
    canary_helper_bucket = get_canary_helper_bucket_name()
    s3_client = boto3.client("s3", region_name=DEFAULT_REGION)
    response = s3_client.get_object(Bucket=canary_helper_bucket, Key="images.json")
    image_uris = json.loads(response["Body"].read().decode("utf-8"))
    return image_uris


def get_canary_default_tag_py3_version(framework, version):
    """
    Currently, only TF2.2 images and above have major/minor python version in their canary tag. Creating this function
    to conditionally choose a python version based on framework version ranges. If we move up to py38, for example,
    this is the place to make the conditional change.
    :param framework: tensorflow1, tensorflow2, mxnet, pytorch
    :param version: fw major.minor version, i.e. 2.2
    :return: default tag python version
    """
    if framework == "tensorflow" or framework == "huggingface_tensorflow":
        if Version("2.2") <= Version(version) < Version("2.6"):
            return "py37"
        if Version("2.6") <= Version(version) < Version("2.8"):
            return "py38"
        if Version("2.8") <= Version(version) < Version("2.12"):
            return "py39"
        if Version(version) >= Version("2.12"):
            return "py310"

    if framework == "mxnet":
        if Version(version) == Version("1.8"):
            return "py37"
        if Version(version) >= Version("1.9"):
            return "py38"

    if framework == "pytorch" or framework == "huggingface_pytorch":
        if Version("1.9") <= Version(version) < Version("1.13"):
            return "py38"
        if Version(version) >= Version("1.13") and Version(version) < Version("2.0"):
            return "py39"
        if Version(version) >= Version("2.0") and Version(version) < Version("2.3"):
            return "py310"
        if Version(version) >= Version("2.3") and Version(version) < Version("2.6"):
            return "py311"
        if Version(version) >= Version("2.6"):
            return "py312"

    return "py3"


def parse_canary_images(framework, region, image_type, customer_type=None):
    """
    Return which canary images to run canary tests on for a given framework and AWS region

    :param framework: ML framework (mxnet, tensorflow, pytorch)
    :param region: AWS region
    :param image_type: training or inference
    :return: dlc_images string (space separated string of image URIs)
    """
    import git

    customer_type = customer_type or get_customer_type()
    customer_type_tag = f"-{customer_type}" if customer_type else ""

    allowed_image_types = ("training", "inference")
    if image_type not in allowed_image_types:
        raise RuntimeError(
            f"Image type is set to {image_type}. It must be set to an allowed image type in {allowed_image_types}"
        )

    canary_type = (
        "graviton_" + framework
        if os.getenv("ARCH_TYPE") == "graviton"
        else "arm64_" + framework if os.getenv("ARCH_TYPE") == "arm64" else framework
    )

    version_regex = {
        "tensorflow": rf"tf(-sagemaker)?{customer_type_tag}-(\d+.\d+)",
        "mxnet": rf"mx(-sagemaker)?{customer_type_tag}-(\d+.\d+)",
        "pytorch": rf"pt(-sagemaker)?{customer_type_tag}-(\d+.\d+)",
        "huggingface_pytorch": r"hf-\S*pt(-sagemaker)?-(\d+.\d+)",
        "huggingface_tensorflow": r"hf-\S*tf(-sagemaker)?-(\d+.\d+)",
        "autogluon": r"ag(-sagemaker)?-(\d+.\d+)\S*-(py\d+)",
        "graviton_tensorflow": rf"tf-graviton(-sagemaker)?{customer_type_tag}-(\d+.\d+)\S*-(py\d+)",
        "graviton_pytorch": rf"pt-graviton(-sagemaker)?{customer_type_tag}-(\d+.\d+)\S*-(py\d+)",
        "graviton_mxnet": rf"mx-graviton(-sagemaker)?{customer_type_tag}-(\d+.\d+)\S*-(py\d+)",
        "arm64_tensorflow": rf"tf-arm64(-sagemaker)?{customer_type_tag}-(\d+.\d+)\S*-(py\d+)",
        "arm64_pytorch": rf"pt-arm64(-sagemaker)?{customer_type_tag}-(\d+.\d+)\S*-(py\d+)",
    }

    # Get tags from repo releases
    repo = git.Repo(os.getcwd(), search_parent_directories=True)

    versions_counter = {}
    pre_populated_py_version = {}

    for tag in repo.tags:
        tag_str = str(tag)
        match = re.search(version_regex[canary_type], tag_str)
        ## The tags not have -py3 will not pass th condition below
        ## This eliminates all the old and testing tags that we are not monitoring.
        if match:
            ## Trcomp tags like v1.0-trcomp-hf-4.21.1-pt-1.11.0-tr-gpu-py38 cause incorrect image URIs to be processed
            ## durign HF PT canary runs. The `if` condition below will prevent any trcomp images to be picked during canary runs of
            ## huggingface_pytorch and huggingface_tensorflow images.
            if (
                "trcomp" in tag_str and "trcomp" not in canary_type and "huggingface" in canary_type
            ) or "tgi" in tag_str:
                continue
            version = match.group(2)
            if not versions_counter.get(version):
                versions_counter[version] = {"tr": False, "inf": False}

            if "tr" not in tag_str and "inf" not in tag_str:
                versions_counter[version]["tr"] = True
                versions_counter[version]["inf"] = True
            elif "tr" in tag_str:
                versions_counter[version]["tr"] = True
            elif "inf" in tag_str:
                versions_counter[version]["inf"] = True

            try:
                python_version_extracted_through_regex = match.group(3)
                if python_version_extracted_through_regex:
                    if version not in pre_populated_py_version:
                        pre_populated_py_version[version] = set()
                    pre_populated_py_version[version].add(python_version_extracted_through_regex)
            except IndexError:
                LOGGER.debug(
                    f"For Framework: {framework} we do not use regex to fetch python version"
                )

    versions = []
    for v, inf_train in versions_counter.items():
        if (inf_train["inf"] and image_type == "inference") or (
            inf_train["tr"] and image_type == "training"
        ):
            versions.append(v)

    # Sort ascending to descending, use lambda to ensure 2.2 < 2.15, for instance
    versions.sort(
        key=lambda version_str: [int(point) for point in version_str.split(".")], reverse=True
    )

    registry = PUBLIC_DLC_REGISTRY
    framework_versions = versions if len(versions) < 4 else versions[:3]
    dlc_images = []
    for fw_version in framework_versions:
        if fw_version in pre_populated_py_version:
            py_versions = pre_populated_py_version[fw_version]
        else:
            py_versions = [get_canary_default_tag_py3_version(canary_type, fw_version)]
        for py_version in py_versions:
            images = {
                "tensorflow": {
                    "training": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/tensorflow-training:{fw_version}-gpu-{py_version}",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/tensorflow-training:{fw_version}-cpu-{py_version}",
                    ],
                    "inference": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/tensorflow-inference:{fw_version}-gpu",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/tensorflow-inference:{fw_version}-cpu",
                    ],
                },
                "mxnet": {
                    "training": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/mxnet-training:{fw_version}-gpu-{py_version}",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/mxnet-training:{fw_version}-cpu-{py_version}",
                    ],
                    "inference": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/mxnet-inference:{fw_version}-gpu-{py_version}",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/mxnet-inference:{fw_version}-cpu-{py_version}",
                    ],
                },
                "pytorch": {
                    "training": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/pytorch-training:{fw_version}-gpu-{py_version}",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/pytorch-training:{fw_version}-cpu-{py_version}",
                    ],
                    "inference": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/pytorch-inference:{fw_version}-gpu-{py_version}",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/pytorch-inference:{fw_version}-cpu-{py_version}",
                    ],
                },
                # TODO: uncomment once cpu training and inference images become available
                "huggingface_pytorch": {
                    "training": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/huggingface-pytorch-training:{fw_version}-gpu-{py_version}",
                        # f"{registry}.dkr.ecr.{region}.amazonaws.com/huggingface-pytorch-training:{fw_version}-cpu-{py_version}",
                    ],
                    "inference": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/huggingface-pytorch-inference:{fw_version}-gpu-{py_version}",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/huggingface-pytorch-inference:{fw_version}-cpu-{py_version}",
                    ],
                },
                "huggingface_tensorflow": {
                    "training": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/huggingface-tensorflow-training:{fw_version}-gpu-{py_version}",
                        # f"{registry}.dkr.ecr.{region}.amazonaws.com/huggingface-tensorflow-training:{fw_version}-cpu-{py_version}",
                    ],
                    "inference": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/huggingface-tensorflow-inference:{fw_version}-gpu-{py_version}",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/huggingface-tensorflow-inference:{fw_version}-cpu-{py_version}",
                    ],
                },
                "autogluon": {
                    "training": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/autogluon-training:{fw_version}-gpu-{py_version}",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/autogluon-training:{fw_version}-cpu-{py_version}",
                    ],
                    "inference": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/autogluon-inference:{fw_version}-gpu-{py_version}",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/autogluon-inference:{fw_version}-cpu-{py_version}",
                    ],
                },
                "graviton_tensorflow": {
                    "training": [],
                    "inference": [
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/tensorflow-inference-graviton:{fw_version}-cpu-{py_version}",
                    ],
                },
                "graviton_pytorch": {
                    "training": [],
                    "inference": [
                        # f"{registry}.dkr.ecr.{region}.amazonaws.com/pytorch-inference-graviton:{fw_version}-gpu-{py_version}",
                        f"{registry}.dkr.ecr.{region}.amazonaws.com/pytorch-inference-graviton:{fw_version}-cpu-{py_version}",
                    ],
                },
                # "arm64_tensorflow": {
                #     "training": [],
                #     "inference": [
                #         f"{registry}.dkr.ecr.{region}.amazonaws.com/tensorflow-inference-arm64:{fw_version}-cpu-{py_version}",
                #     ],
                # },
                # "arm64_pytorch": {
                #     "training": [
                #         f"{registry}.dkr.ecr.{region}.amazonaws.com/pytorch-training-arm64:{fw_version}-gpu-{py_version}",
                #     ],
                #     "inference": [
                #         f"{registry}.dkr.ecr.{region}.amazonaws.com/pytorch-inference-arm64:{fw_version}-gpu-{py_version}",
                #         f"{registry}.dkr.ecr.{region}.amazonaws.com/pytorch-inference-arm64:{fw_version}-cpu-{py_version}",
                #     ],
                # },
            }

            # ec2 Images have an additional "ec2" tag to distinguish them from the regular "sagemaker" tag
            if customer_type == "ec2":
                dlc_images += [f"{img}-ec2" for img in images[canary_type][image_type]]
            else:
                dlc_images += images[canary_type][image_type]

    dlc_images.sort()
    return " ".join(dlc_images)


def setup_sm_benchmark_tf_train_env(resources_location, setup_tf1_env, setup_tf2_env):
    """
    Create a virtual environment for benchmark tests if it doesn't already exist, and download all necessary scripts

    :param resources_location: <str> directory in which test resources should be placed
    :param setup_tf1_env: <bool> True if tf1 resources need to be setup
    :param setup_tf2_env: <bool> True if tf2 resources need to be setup
    :return: absolute path to the location of the virtual environment
    """
    ctx = Context()

    tf_resource_dir_list = []
    if setup_tf1_env:
        tf_resource_dir_list.append("tensorflow1")
    if setup_tf2_env:
        tf_resource_dir_list.append("tensorflow2")

    for resource_dir in tf_resource_dir_list:
        with ctx.cd(os.path.join(resources_location, resource_dir)):
            if not os.path.isdir(os.path.join(resources_location, resource_dir, "horovod")):
                # v0.19.4 is the last version for which horovod example tests are py2 compatible
                ctx.run("git clone -b v0.19.4 https://github.com/horovod/horovod.git")
            if not os.path.isdir(
                os.path.join(resources_location, resource_dir, "deep-learning-models")
            ):
                # We clone branch tf2 for both 1.x and 2.x tests because tf2 branch contains all necessary files
                ctx.run(f"git clone -b tf2 https://github.com/aws-samples/deep-learning-models.git")

    venv_dir = os.path.join(resources_location, "sm_benchmark_venv")
    if not os.path.isdir(venv_dir):
        ctx.run(f"virtualenv {venv_dir}")
        with ctx.prefix(f"source {venv_dir}/bin/activate"):
            ctx.run("pip install 'sagemaker>=2,<3' awscli boto3 botocore six==1.11")

            # SageMaker TF estimator is coded to only accept framework versions up to 2.1.0 as py2 compatible.
            # Fixing this through the following changes:
            estimator_location = ctx.run(
                "echo $(pip3 show sagemaker |grep 'Location' |sed s/'Location: '//g)/sagemaker/tensorflow/estimator.py"
            ).stdout.strip("\n")
            system = ctx.run("uname -s").stdout.strip("\n")
            sed_input_arg = "'' " if system == "Darwin" else ""
            ctx.run(f"sed -i {sed_input_arg}'s/\[2, 1, 0\]/\[2, 1, 1\]/g' {estimator_location}")
    return venv_dir


def setup_sm_benchmark_mx_train_env(resources_location):
    """
    Create a virtual environment for benchmark tests if it doesn't already exist, and download all necessary scripts
    :param resources_location: <str> directory in which test resources should be placed
    :return: absolute path to the location of the virtual environment
    """
    ctx = Context()

    venv_dir = os.path.join(resources_location, "sm_benchmark_venv")
    if not os.path.isdir(venv_dir):
        ctx.run(f"virtualenv {venv_dir}")
        with ctx.prefix(f"source {venv_dir}/bin/activate"):
            ctx.run("pip install sagemaker awscli boto3 botocore")
    return venv_dir


def setup_sm_benchmark_hf_infer_env(resources_location):
    """
    Create a virtual environment for benchmark tests if it doesn't already exist, and download all necessary scripts
    :param resources_location: <str> directory in which test resources should be placed
    :return: absolute path to the location of the virtual environment
    """
    ctx = Context()

    venv_dir = os.path.join(resources_location, "sm_benchmark_hf_venv")
    if not os.path.isdir(venv_dir):
        ctx.run(f"python3 -m virtualenv {venv_dir}")
    return venv_dir


def get_account_id_from_image_uri(image_uri):
    """
    Find the account ID where the image is located

    :param image_uri: <str> ECR image URI
    :return: <str> AWS Account ID
    """
    return image_uri.split(".")[0]


def get_region_from_image_uri(image_uri):
    """
    Find the region where the image is located

    :param image_uri: <str> ECR image URI
    :return: <str> AWS Region Name
    """
    region_pattern = r"(us(-gov)?|af|ap|ca|cn|eu|il|me|sa)-(central|(north|south)?(east|west)?)-\d+"
    region_search = re.search(region_pattern, image_uri)
    assert region_search, f"{image_uri} must have region that matches {region_pattern}"
    return region_search.group()


def get_unique_name_from_tag(image_uri):
    """
    Return the unique from the image tag.
    :param image_uri: ECR image URI
    :return: unique name
    """
    return re.sub("[^A-Za-z0-9]+", "", image_uri)


def get_image_type_from_tag(image_uri):
    """
    Extract the image type (training, inference, or general) from the image URI.

    :param image_uri: str ECR image URI
    :return: str "training", "inference", or "general"
    """
    if "training" in image_uri:
        return "training"
    elif "inference" in image_uri:
        return "inference"
    else:
        return "general"


def get_image_arch_type_from_tag(image_uri):
    """
    All images are assumed by default to be x86, unless they are graviton/arm64 type
    :param image_uri: str ECR image URI
    :return: str "graviton" or "arm64" or "x86"
    """
    return "graviton" if "graviton" in image_uri else "arm64" if "arm64" in image_uri else "x86"


def get_framework_and_version_from_tag(image_uri):
    """
    Return the framework and version from the image tag.

    :param image_uri: ECR image URI
    :return: framework name, framework version
    """
    tested_framework = get_framework_from_image_uri(image_uri)
    allowed_frameworks = (
        "huggingface_tensorflow_trcomp",
        "huggingface_pytorch_trcomp",
        "huggingface_tensorflow",
        "huggingface_pytorch",
        "stabilityai_pytorch",
        "pytorch_trcomp",
        "tensorflow",
        "pytorch",
        "autogluon",
    )

    if not tested_framework:
        raise RuntimeError(
            f"Cannot find framework in image uri {image_uri} "
            f"from allowed frameworks {allowed_frameworks}"
        )

    tag_framework_version = re.search(r"(\d+(\.\d+){1,2})", image_uri).groups()[0]

    return tested_framework, tag_framework_version


def get_neuron_sdk_version_from_tag(image_uri):
    """
    Return the neuron sdk version from the image tag.
    :param image_uri: ECR image URI
    :return: neuron sdk version
    """
    neuron_sdk_version = None

    if "sdk" in image_uri:
        neuron_sdk_version = re.search(r"sdk([\d\.]+)", image_uri).group(1)

    return neuron_sdk_version


def get_neuron_release_manifest(sdk_version):
    """
    Returns a dictionary which maps a package name (e.g. "tensorflow-neuronx") to all the version numbers available in and SDK release

    :param sdk_version: Neuron SDK version
    :return: { "tensorflow_neuronx": ["15.5.1.1.1.1.1", "2.10.1.1.1.1.1", ...], ... }
    """
    NEURON_RELEASE_MANIFEST_URL = "https://raw.githubusercontent.com/aws-neuron/aws-neuron-sdk/master/src/helperscripts/n2-manifest.json"

    manifest = requests.get(NEURON_RELEASE_MANIFEST_URL)
    assert manifest.text, f"Neuron manifest file at {NEURON_RELEASE_MANIFEST_URL} is empty"

    manifest_data = json.loads(manifest.text)

    if "neuron_releases" not in manifest_data:
        raise KeyError(f"neuron_releases not found in manifest_data\n{json.dumps(manifest_data)}")

    release_array = manifest_data["neuron_releases"]
    release = None

    for current_release in release_array:
        if "neuron_version" not in current_release:
            continue
        if current_release["neuron_version"] == sdk_version:
            release = current_release
            break

    if release is None:
        raise KeyError(
            f"cannot find neuron neuron sdk version {sdk_version} "
            f"in releases:\n{json.dumps(release_array)}"
        )

    if "packages" not in release:
        raise KeyError(f"packages not found in release data\n{json.dumps(release)}")

    package_array = release["packages"]
    package_versions = {}
    for package in package_array:
        package_name = package["name"]
        package_version = package["version"]
        if package_name not in package_versions:
            package_versions[package_name] = []
        package_versions[package_name].append(package_version)

    return package_versions


def get_transformers_version_from_image_uri(image_uri):
    """
    Utility function to get the HuggingFace transformers version from an image uri

    @param image_uri: ECR image uri
    @return: HuggingFace transformers version, or ""
    """
    transformers_regex = re.compile(r"transformers(\d+.\d+.\d+)")
    transformers_in_img_uri = transformers_regex.search(image_uri)
    if transformers_in_img_uri:
        return transformers_in_img_uri.group(1)
    return ""


def get_os_version_from_image_uri(image_uri):
    """
    Currently only ship ubuntu versions

    @param image_uri: ECR image URI
    @return: OS version, or ""
    """
    os_version_regex = re.compile(r"ubuntu\d+.\d+")
    os_version_in_img_uri = os_version_regex.search(image_uri)
    if os_version_in_img_uri:
        return os_version_in_img_uri.group()
    return ""


def get_framework_from_image_uri(image_uri):
    framework_map = {
        "huggingface-tensorflow-trcomp": "huggingface_tensorflow_trcomp",
        "huggingface-tensorflow": "huggingface_tensorflow",
        "huggingface-pytorch-trcomp": "huggingface_pytorch_trcomp",
        "pytorch-trcomp": "pytorch_trcomp",
        "huggingface-pytorch": "huggingface_pytorch",
        "stabilityai-pytorch": "stabilityai_pytorch",
        "mxnet": "mxnet",
        "pytorch": "pytorch",
        "tensorflow": "tensorflow",
        "autogluon": "autogluon",
        "base": "base",
        "vllm": "vllm",
    }

    for image_pattern, framework in framework_map.items():
        if image_pattern in image_uri:
            return framework

    return None


def is_trcomp_image(image_uri):
    framework = get_framework_from_image_uri(image_uri)
    return "trcomp" in framework


def get_all_the_tags_of_an_image_from_ecr(ecr_client, image_uri):
    """
    Uses ecr describe to generate all the tags of an image.

    :param ecr_client: boto3 Client for ECR
    :param image_uri: str Image URI
    :return: list, All the image tags
    """
    account_id = get_account_id_from_image_uri(image_uri)
    image_repo_name, image_tag = get_repository_and_tag_from_image_uri(image_uri)
    response = ecr_client.describe_images(
        registryId=account_id,
        repositoryName=image_repo_name,
        imageIds=[
            {"imageTag": image_tag},
        ],
    )
    return response["imageDetails"][0]["imageTags"]


def get_image_push_time_from_ecr(ecr_client, image_uri):
    """
    Uses ecr describe to get the time when an image was pushed in the ECR.

    :param ecr_client: boto3 Client for ECR
    :param image_uri: str Image URI
    :return: datetime.datetime Object, Returns time
    """
    account_id = get_account_id_from_image_uri(image_uri)
    image_repo_name, image_tag = get_repository_and_tag_from_image_uri(image_uri)
    response = ecr_client.describe_images(
        registryId=account_id,
        repositoryName=image_repo_name,
        imageIds=[
            {"imageTag": image_tag},
        ],
    )
    return response["imageDetails"][0]["imagePushedAt"]


def get_sha_of_an_image_from_ecr(ecr_client, image_uri):
    """
    Uses ecr describe to get SHA of an image.

    :param ecr_client: boto3 Client for ECR
    :param image_uri: str Image URI
    :return: str, Image SHA that looks like sha256:1ab...
    """
    account_id = get_account_id_from_image_uri(image_uri)
    image_repo_name, image_tag = get_repository_and_tag_from_image_uri(image_uri)
    response = ecr_client.describe_images(
        registryId=account_id,
        repositoryName=image_repo_name,
        imageIds=[
            {"imageTag": image_tag},
        ],
    )
    return response["imageDetails"][0]["imageDigest"]


def get_cuda_version_from_tag(image_uri):
    """
    Return the cuda version from the image tag as cuXXX
    :param image_uri: ECR image URI
    :return: cuda version as cuXXX
    """
    cuda_framework_version = None
    cuda_str = ["cu", "gpu"]
    image_region = get_region_from_image_uri(image_uri)
    ecr_client = boto3.Session(region_name=image_region).client("ecr")
    _, local_image_tag = get_repository_and_tag_from_image_uri(image_uri)
    all_image_tags = [local_image_tag]
    try:
        all_image_tags = get_all_the_tags_of_an_image_from_ecr(ecr_client, image_uri)
    except ecr_client.exceptions.ImageNotFoundException as e:
        LOGGER.info(
            f"Image {image_uri} not found in ECR - this is expected when the image is not pushed yet. Client Logs: {e}"
        )

    for image_tag in all_image_tags:
        if all(keyword in image_tag for keyword in cuda_str):
            cuda_framework_version = re.search(r"(cu\d+)-", image_tag).groups()[0]
            return cuda_framework_version

    if "gpu" in image_uri:
        raise CudaVersionTagNotFoundException()
    else:
        return None


def get_synapseai_version_from_tag(image_uri):
    """
    Return the synapseai version from the image tag.
    :param image_uri: ECR image URI
    :return: synapseai version
    """
    synapseai_version = None

    synapseai_str = ["synapseai", "hpu"]
    if all(keyword in image_uri for keyword in synapseai_str):
        synapseai_version = re.search(r"synapseai(\d+(\.\d+){2})", image_uri).groups()[0]

    return synapseai_version


def get_job_type_from_image(image_uri):
    """
    Return the Job type from the image tag.

    :param image_uri: ECR image URI
    :return: Job Type
    """
    tested_job_type = None
    allowed_job_types = ("training", "inference", "base", "vllm")
    for job_type in allowed_job_types:
        if job_type in image_uri:
            tested_job_type = job_type
            break

    if not tested_job_type and "eia" in image_uri:
        tested_job_type = "inference"

    if not tested_job_type:
        raise RuntimeError(
            f"Cannot find Job Type in image uri {image_uri} "
            f"from allowed frameworks {allowed_job_types}"
        )

    return tested_job_type


def get_repository_and_tag_from_image_uri(image_uri):
    """
    Return the name of the repository holding the image

    :param image_uri: URI of the image
    :return: <str> repository name
    """
    repository_uri, tag = image_uri.split(":")
    _, repository_name = repository_uri.split("/")
    return repository_name, tag


def get_processor_from_image_uri(image_uri):
    """
    Return processor from the image URI

    Assumes image uri includes -<processor> in it's tag, where <processor> is one of cpu, gpu or eia.

    :param image_uri: ECR image URI
    :return: cpu, gpu, eia, neuron or hpu
    """
    allowed_processors = ["eia", "neuronx", "neuron", "cpu", "gpu", "hpu"]

    for processor in allowed_processors:
        match = re.search(rf"({processor})", image_uri)
        if match:
            return match.group(1)
    raise RuntimeError("Cannot find processor")


def get_python_version_from_image_uri(image_uri):
    """
    Return the python version from the image URI

    :param image_uri: ECR image URI
    :return: str py36, py37, py38, etc., based information available in image URI
    """
    python_version_search = re.search(r"py\d+", image_uri)
    if not python_version_search:
        raise MissingPythonVersionException(
            f"{image_uri} does not have python version in the form 'py\\d+'"
        )
    python_version = python_version_search.group()
    return "py36" if python_version == "py3" else python_version


def get_pytorch_version_from_autogluon_image(image):
    """
    Extract the PyTorch version from an AutoGluon container.
    :param image: ECR image URI
    :return: PyTorch short version or None if detection fails
    """
    ctx = Context()
    container_name = get_container_name("pytorch-version-check", image)
    try:
        start_container(container_name, image, ctx)
        pytorch_version_output = run_cmd_on_container(
            container_name, ctx, "import torch; print(torch.__version__)", executable="python"
        )
        # Parse "2.5.1+cpu" -> "2.5"
        pytorch_full_version = pytorch_version_output.stdout.strip()
        pytorch_short_version = re.search(r"(\d+\.\d+)", pytorch_full_version).group(0)
        return pytorch_short_version
    except Exception as e:
        LOGGER.error(f"Failed to detect PyTorch version in AutoGluon image: {e}")
        return None
    finally:
        stop_and_remove_container(container_name, ctx)


def get_buildspec_path(dlc_path):
    """
    Get buildspec file that should be used in testing a particular DLC image. This file is normally
    configured as an environment variable on PR and Mainline test jobs. If it isn't configured,
    construct a relative path to the buildspec yaml file by iterative checking on the existence of
    a specific version file for the framework being tested. Possible options include:
    [buildspec-[Major]-[Minor]-[Patch].yml, buildspec-[Major]-[Minor].yml, buildspec-[Major].yml, buildspec.yml]
    :param dlc_path: path to the DLC test folder
    """
    buildspec_path = None
    if is_pr_context() and os.getenv("FRAMEWORK_BUILDSPEC_FILE"):
        buildspec_path = os.path.join(dlc_path, os.getenv("FRAMEWORK_BUILDSPEC_FILE"))
    elif os.getenv("CODEBUILD_SRC_DIR_DLC_BUILDSPEC_FILE"):
        buildspec_path = os.path.join(
            os.getenv("CODEBUILD_SRC_DIR_DLC_BUILDSPEC_FILE"), "framework-buildspec.yml"
        )

    assert os.path.exists(buildspec_path), f"buildspec_path - {buildspec_path} - is invalid"

    return buildspec_path


def get_container_name(prefix, image_uri):
    """
    Create a unique container name based off of a test related prefix and the image uri
    :param prefix: test related prefix, like "emacs" or "pip-check"
    :param image_uri: ECR image URI
    :return: container name
    """
    return f"{prefix}-{image_uri.split('/')[-1].replace('.', '-').replace(':', '-')}"


def stop_and_remove_container(container_name, context):
    """
    Helper function to stop a container locally
    :param container_name: Name of the docker container
    :param context: Invoke context object
    """
    context.run(
        f"docker rm -f {container_name}",
        hide=True,
    )


def start_container(container_name, image_uri, context):
    """
    Helper function to start a container locally
    :param container_name: Name of the docker container
    :param image_uri: ECR image URI
    :param context: Invoke context object
    """
    context.run(
        f"docker run --entrypoint='/bin/bash' --name {container_name} -itd {image_uri}",
        hide=True,
    )


def run_cmd_on_container(
    container_name,
    context,
    cmd,
    executable="bash",
    warn=False,
    hide=True,
    timeout=60,
    asynchronous=False,
):
    """
    Helper function to run commands on a locally running container
    :param container_name: Name of the docker container
    :param context: ECR image URI
    :param cmd: Command to run on the container
    :param executable: Executable to run on the container (bash or python)
    :param warn: Whether to only warn as opposed to exit if command fails
    :param hide: Hide some or all of the stdout/stderr from running the command
    :param timeout: Timeout in seconds for command to be executed
    :param asynchronous: False by default, set to True if command should run asynchronously
        Refer to https://docs.pyinvoke.org/en/latest/api/runners.html#invoke.runners.Runner.run for
        more details on running asynchronous commands.
    :return: invoke output, can be used to parse stdout, etc
    """
    if executable not in ("bash", "python"):
        LOGGER.warning(
            f"Unrecognized executable {executable}. It will be run as {executable} -c '{cmd}'"
        )
    return context.run(
        f"docker exec --user root {container_name} {executable} -c '{cmd}'",
        hide=hide,
        warn=warn,
        timeout=timeout,
        asynchronous=asynchronous,
    )


def uniquify_list_of_dict(list_of_dict):
    """
    Takes list_of_dict as an input and returns a list of dict such that each dict is only present
    once in the returned list. Runs an operation that is similar to list(set(input_list)). However,
    for list_of_dict, it is not possible to run the operation directly.

    :param list_of_dict: List(dict)
    :return: List(dict)
    """
    list_of_string = [json.dumps(dict_element, sort_keys=True) for dict_element in list_of_dict]
    unique_list_of_string = list(set(list_of_string))
    unique_list_of_string.sort()
    list_of_dict_to_return = [json.loads(str_element) for str_element in unique_list_of_string]
    return list_of_dict_to_return


def uniquify_list_of_complex_datatypes(list_of_complex_datatypes):
    assert all(
        type(element) == type(list_of_complex_datatypes[0]) for element in list_of_complex_datatypes
    ), f"{list_of_complex_datatypes} has multiple types"
    if list_of_complex_datatypes:
        if isinstance(list_of_complex_datatypes[0], dict):
            return uniquify_list_of_dict(list_of_complex_datatypes)
        if dataclasses.is_dataclass(list_of_complex_datatypes[0]):
            type_of_dataclass = type(list_of_complex_datatypes[0])
            list_of_dict = json.loads(
                json.dumps(list_of_complex_datatypes, cls=EnhancedJSONEncoder)
            )
            uniquified_list = uniquify_list_of_dict(list_of_dict=list_of_dict)
            return [
                type_of_dataclass(**uniquified_list_dict_element)
                for uniquified_list_dict_element in uniquified_list
            ]
        raise "Not implemented"
    return list_of_complex_datatypes


def check_if_two_dictionaries_are_equal(dict1, dict2, ignore_keys=[]):
    """
    Compares if 2 dictionaries are equal or not. The ignore_keys argument is used to provide
    a list of keys that are ignored while comparing the dictionaries.

    :param dict1: dict
    :param dict2: dict
    :param ignore_keys: list[str], keys that are ignored while comparison
    """
    dict1_filtered = {k: v for k, v in dict1.items() if k not in ignore_keys}
    dict2_filtered = {k: v for k, v in dict2.items() if k not in ignore_keys}
    return dict1_filtered == dict2_filtered


def get_tensorflow_model_base_path(image_uri):
    """
    Retrieve model base path based on version of TensorFlow
    Requirement: Model defined in TENSORFLOW_MODELS_PATH should be hosted in S3 location for TF version less than 2.6.
                 Starting TF2.7, the models are referred locally as the support for S3 is moved to a separate python package `tensorflow-io`
    :param image_uri: ECR image URI
    :return: <string> model base path
    """
    if is_below_framework_version("2.7", image_uri, "tensorflow"):
        model_base_path = TENSORFLOW_MODELS_BUCKET
    else:
        model_base_path = f"/tensorflow_model/"
    return model_base_path


def build_tensorflow_inference_command_tf27_and_above(
    model_name, entrypoint="/usr/bin/tf_serving_entrypoint.sh"
):
    """
    Construct the command to download tensorflow model from S3 and start tensorflow model server
    :param model_name:
    :return: <list> command to send to the container
    """
    inference_command = f"mkdir -p /tensorflow_model && aws s3 sync {TENSORFLOW_MODELS_BUCKET}/{model_name}/ /tensorflow_model/{model_name} && {entrypoint}"
    return inference_command


def get_tensorflow_inference_environment_variables(model_name, model_base_path):
    """
    Get method for environment variables for tensorflow inference for EC2 and ECS
    :param model_name:
    :return: <list> JSON
    """
    tensorflow_inference_environment_variables = [
        {"name": "MODEL_NAME", "value": model_name},
        {"name": "MODEL_BASE_PATH", "value": model_base_path},
    ]

    return tensorflow_inference_environment_variables


def get_eks_k8s_test_type_label(image_uri):
    """
    Get node label required for k8s job to be scheduled on compatible EKS node
    :param image_uri: ECR image URI
    :return: <string> node label
    """
    if "graviton" in image_uri or "arm64" in image_uri:
        # using the graviton nodegroup (c6g.4xlarge) for both graviton and arm64 for now
        test_type = "graviton"
    elif "neuron" in image_uri:
        test_type = "neuron"
    else:
        test_type = "gpu"
    return test_type


def execute_env_variables_test(image_uri, env_vars_to_test, container_name_prefix):
    """
    Based on a dictionary of ENV_VAR: val, test that the enviornment variables are correctly set in the container.

    @param image_uri: ECR image URI
    @param env_vars_to_test: dict {"ENV_VAR": "env_var_expected_value"}
    @param container_name_prefix: container name prefix describing test
    """
    ctx = Context()
    container_name = get_container_name(container_name_prefix, image_uri)

    start_container(container_name, image_uri, ctx)
    for var, expected_val in env_vars_to_test.items():
        output = run_cmd_on_container(container_name, ctx, f"echo ${var}")
        actual_val = output.stdout.strip()
        if actual_val:
            assertion_error_sentence = f"It is currently set to {actual_val}."
        else:
            assertion_error_sentence = "It is currently not set."
        assert (
            actual_val == expected_val
        ), f"Environment variable {var} is expected to be {expected_val}. {assertion_error_sentence}."
    stop_and_remove_container(container_name, ctx)


def is_image_available_locally(image_uri):
    """
    Check if the image exists locally.

    :param image_uri: str, image that needs to be checked
    :return: bool, True if image exists locally, otherwise false
    """
    run_output = run(f"docker inspect {image_uri}", hide=True, warn=True)
    return run_output.ok


def get_contributor_from_image_uri(image_uri):
    """
    Return contributor name if it is present in the image URI

    @param image_uri: ECR image uri
    @return: contributor name, or ""
    """
    # Key value pair of contributor_identifier_in_image_uri: contributor_name
    contributors = {"huggingface": "huggingface", "habana": "habana"}
    for contributor_identifier_in_image_uri, contributor_name in contributors.items():
        if contributor_identifier_in_image_uri in image_uri:
            return contributor_name
    return ""


def get_labels_from_ecr_image(image_uri, region):
    """
    Get ecr image labels from ECR

    @param image_uri: ECR image URI to get labels from
    @param region: AWS region
    @return: list of labels attached to ECR image URI
    """
    ecr_client = boto3.client("ecr", region_name=region)

    image_repository, image_tag = get_repository_and_tag_from_image_uri(image_uri)
    # Using "acceptedMediaTypes" on the batch_get_image request allows the returned image information to
    # provide the ECR Image Manifest in the specific format that we need, so that the image LABELS can be found
    # on the manifest. The default format does not return the image LABELs.
    response = ecr_client.batch_get_image(
        repositoryName=image_repository,
        imageIds=[{"imageTag": image_tag}],
        acceptedMediaTypes=["application/vnd.docker.distribution.manifest.v1+json"],
    )
    if not response.get("images"):
        raise KeyError(
            f"Failed to get images through ecr_client.batch_get_image response for image {image_repository}:{image_tag}"
        )
    elif not response["images"][0].get("imageManifest"):
        raise KeyError(
            f"imageManifest not found in ecr_client.batch_get_image response:\n{response['images']}"
        )

    manifest_str = response["images"][0]["imageManifest"]
    # manifest_str is a json-format string
    manifest = json.loads(manifest_str)
    image_metadata = json.loads(manifest["history"][0]["v1Compatibility"])
    labels = image_metadata["config"]["Labels"]

    return labels


def generate_unique_dlc_name(image):
    # handle retrevial of repo name and remove test type from it
    return get_ecr_repo_name(image).replace("-training", "").replace("-inference", "")


def get_ecr_scan_allowlist_path(image_uri, python_version=None):
    """
    This method has the logic to extract the ecr scan allowlist path for each image. This method earlier existed in another file and
    has simply been relocated to this one.

    :param image_uri: str, Image URI
    :param python_version: str, python_version
    :return: str, ecr_scan_allowlist path
    """
    dockerfile_location = get_dockerfile_path_for_image(image_uri, python_version=python_version)
    image_scan_allowlist_path = dockerfile_location + ".os_scan_allowlist.json"
    if (
        not any(image_type in image_uri for image_type in ["neuron", "eia"])
        and is_covered_by_ec2_sm_split(image_uri)
        and is_ec2_sm_in_same_dockerfile(image_uri)
    ):
        if is_ec2_image(image_uri):
            image_scan_allowlist_path = image_scan_allowlist_path.replace(
                "Dockerfile", "Dockerfile.ec2"
            )
        else:
            image_scan_allowlist_path = image_scan_allowlist_path.replace(
                "Dockerfile", "Dockerfile.sagemaker"
            )

    # Each example image (tied to CUDA version/OS version/other variants) can have its own list of vulnerabilities,
    # which means that we cannot have just a single allowlist for all example images for any framework version.
    if "example" in image_uri:
        # The extracted dockerfile_location in case of example image points to the base gpu image on top of which the
        # example image was built. The dockerfile_location looks like
        # tensorflow/training/docker/2.7/py3/cu112/Dockerfile.ec2.gpu.example.os_scan_allowlist.json
        # We want to change the parent folder such that it points from cu112 folder to example folder and
        # looks like tensorflow/training/docker/2.7/py3/example/Dockerfile.gpu.example.os_scan_allowlist.json
        dockerfile_location = dockerfile_location.replace(".ec2.", ".")
        base_gpu_image_path = Path(dockerfile_location)
        image_scan_allowlist_path = os.path.join(
            str(base_gpu_image_path.parent.parent), "example", base_gpu_image_path.name
        )
        image_scan_allowlist_path += ".example.os_scan_allowlist.json"
    return image_scan_allowlist_path


def get_installed_python_packages_with_version(docker_exec_command: str):
    """
    This method extracts all the installed python packages and their versions from a DLC.

    :param docker_exec_command: str, The Docker exec command for an already running container.
    :return: Dict, Dictionary with key=package_name and value=package_version in str
    """
    package_version_dict = {}

    python_cmd_to_extract_package_set = "pip list --format json"

    run_output = run(f"{docker_exec_command} {python_cmd_to_extract_package_set}", hide=True)
    list_of_package_data_dicts = json.loads(run_output.stdout)

    for package_data_dict in list_of_package_data_dicts:
        package_name = package_data_dict["name"].lower().replace("_", "-")
        if package_name in package_version_dict:
            raise Exception(
                f""" Package {package_name} existing multiple times in {list_of_package_data_dicts}"""
            )
        package_version_dict[package_name] = package_data_dict["version"]

    return package_version_dict


def get_installed_python_packages_using_image_uri(context, image_uri, container_name=""):
    """
    This method returns the python package versions that are installed within and image_uri. This method
    handles all the overhead of creating a container for the image and then running the command on top of
    it and then removing the container.

    :param context: Invoke context object
    :param image_uri: str, Image URI
    :param container_name: str, Custom name for the container
    :return: Dict, Dictionary with key=package_name and value=package_version in str
    """
    container_name = container_name or get_container_name(
        f"py-version-extraction-{uuid.uuid4()}", image_uri
    )
    start_container(container_name, image_uri, context)
    docker_exec_command_for_current_image = f"""docker exec --user root {container_name}"""
    current_image_package_version_dict = get_installed_python_packages_with_version(
        docker_exec_command=docker_exec_command_for_current_image
    )
    stop_and_remove_container(container_name=container_name, context=context)
    return current_image_package_version_dict


def get_image_spec_from_buildspec(image_uri, dlc_folder_path):
    """
    This method reads the BuildSpec file for the given image_uri and returns that image_spec within
    the BuildSpec file that corresponds to the given image_uri.

    :param image_uri: str, Image URI
    :param dlc_folder_path: str, Path of the DLC folder on the current host
    :return: dict, the image_spec dictionary corresponding to the given image
    """
    from src.buildspec import Buildspec

    _, image_tag = get_repository_and_tag_from_image_uri(image_uri)
    buildspec_path = get_buildspec_path(dlc_folder_path)
    buildspec_def = Buildspec()
    buildspec_def.load(buildspec_path)
    matched_image_spec = None

    for _, image_spec in buildspec_def["images"].items():
        # If an image_spec in the buildspec matches the input image tag:
        #   - if there is no pre-existing matched image spec, choose the image_spec
        #   - if there is a pre-existing matched image spec, choose the image_spec that has
        #     a larger overlap with the input image tag.
        if image_tag.startswith(image_spec["tag"]):
            if not matched_image_spec or len(matched_image_spec["tag"]) < len(image_spec["tag"]):
                matched_image_spec = image_spec

    if not matched_image_spec:
        raise ValueError(f"No corresponding entry found for {image_uri} in {buildspec_path}")

    return matched_image_spec


def get_dlami_id(region):
    """
    Returns the appropriate base DLAMI based on region.
    Args:
        instance_type: The EC2 instance type (not used in current implementation)
        region: AWS region (us-west-2 or us-east-1)

    Returns:
        The appropriate base DLAMI constant for the specified region

    Raises:
        ValueError: If region is not supported
    """
    if region == "us-west-2":
        LOGGER.info(f"using AL2023_BASE_DLAMI_US_WEST_2 - : {AL2023_BASE_DLAMI_US_WEST_2}")
        return AL2023_BASE_DLAMI_US_WEST_2
    elif region == "us-east-1":
        LOGGER.info(f"using AL2023_BASE_DLAMI_US_EAST_1 - : {AL2023_BASE_DLAMI_US_EAST_1}")
        return AL2023_BASE_DLAMI_US_EAST_1
    else:
        raise ValueError(
            f"Unsupported region: {region}. Only 'us-west-2' and 'us-east-1' are supported."
        )
