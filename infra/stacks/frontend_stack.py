"""Frontend hosting: private S3 bucket + CloudFront with Origin Access Control."""
from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    aws_s3 as s3,
    aws_cloudfront as cf,
    aws_cloudfront_origins as origins,
)
from constructs import Construct


class FrontendStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, api_url: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        distribution = cf.Distribution(
            self,
            "SiteDist",
            default_root_object="index.html",
            default_behavior=cf.BehaviorOptions(
                origin=origins.S3Origin(bucket),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,
            ),
            error_responses=[
                cf.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                )
            ],
            price_class=cf.PriceClass.PRICE_CLASS_100,
        )

        CfnOutput(self, "SiteBucketName", value=bucket.bucket_name)
        CfnOutput(self, "SiteUrl", value=f"https://{distribution.domain_name}")
        CfnOutput(self, "ApiUrl", value=api_url)
