"""Provider-agnostic image build and push abstractions.

Providers inject registry auth (ECR credentials, GCR service account, etc.)
via a callable on the pusher. The builder and pusher themselves only know
about Docker's CLI.
"""

from .builder import ImageBuilder, ImageBuildSpec, ImageBuildError
from .pusher import ImagePusher, ImagePushError

__all__ = [
    "ImageBuilder",
    "ImageBuildSpec",
    "ImageBuildError",
    "ImagePusher",
    "ImagePushError",
]
