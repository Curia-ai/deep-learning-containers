import os, sys
import logging
from typing import List

from test.test_utils import get_dlc_images
from test.vllm.eks.eks_test import test_vllm_on_eks

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)
LOGGER.addHandler(logging.StreamHandler(sys.stdout))


def run_platform_tests(platform: str, images: List[str], commit_id: str, ipv6_enabled: bool):
    """
    Run tests for a specific platform
    """
    LOGGER.info(f"Running {platform} tests")
    if platform == "ec2":
        # Placeholder for EC2 tests
        pass
    elif platform == "eks":
        LOGGER.info("Running EKS tests")
        try:
            test_vllm_on_eks(images[0])
            LOGGER.info("EKS vLLM tests completed successfully")
        except Exception as e:
            LOGGER.error(f"EKS vLLM tests failed: {str(e)}")
            raise


def test():
    LOGGER.info("Triggering test from vllm")
    test_type = os.getenv("TEST_TYPE")

    LOGGER.info(f"TEST_TYPE: {test_type}")

    executor_mode = os.getenv("EXECUTOR_MODE", "False").lower() == "true"
    dlc_images = os.getenv("DLC_IMAGE") if executor_mode else get_dlc_images()

    ipv6_enabled = os.getenv("ENABLE_IPV6_TESTING", "false").lower() == "true"
    os.environ["ENABLE_IPV6_TESTING"] = "true" if ipv6_enabled else "false"

    commit_id = os.getenv("CODEBUILD_RESOLVED_SOURCE_VERSION", default="unrecognised_commit_id")
    LOGGER.info(f"Commit ID: {commit_id}")

    LOGGER.info(f"Images tested: {dlc_images}")
    all_image_list = dlc_images.split(" ")
    standard_images_list = [image_uri for image_uri in all_image_list if "example" not in image_uri]
    LOGGER.info(f"\nImages URIs:\n{standard_images_list}")

    run_platform_tests(
        platform=test_type,
        images=standard_images_list,
        commit_id=commit_id,
        ipv6_enabled=ipv6_enabled,
    )


if __name__ == "__main__":
    test()
