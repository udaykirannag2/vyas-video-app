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

        # Rewrite /path and /path/ → /path/index.html so Next.js static-export
        # sub-pages are served correctly (S3 REST API has no directory indexes).
        url_rewrite = cf.Function(
            self,
            "UrlRewriteFn",
            code=cf.FunctionCode.from_inline(
                "function handler(event){"
                "var r=event.request,u=r.uri;"
                "if(u.endsWith('/'))r.uri+=u==='/'?'index.html':'index.html';"
                "else if(!u.includes('.'))r.uri+='/index.html';"
                "return r;}"
            ),
        )

        distribution = cf.Distribution(
            self,
            "SiteDist",
            default_root_object="index.html",
            default_behavior=cf.BehaviorOptions(
                origin=origins.S3Origin(bucket),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,
                function_associations=[
                    cf.FunctionAssociation(
                        function=url_rewrite,
                        event_type=cf.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            error_responses=[
                cf.ErrorResponse(
                    http_status=403,
                    response_http_status=404,
                    response_page_path="/404.html",
                ),
                cf.ErrorResponse(
                    http_status=404,
                    response_http_status=404,
                    response_page_path="/404.html",
                ),
            ],
            price_class=cf.PriceClass.PRICE_CLASS_100,
        )

        CfnOutput(self, "SiteBucketName", value=bucket.bucket_name)
        CfnOutput(self, "SiteUrl", value=f"https://{distribution.domain_name}")
        CfnOutput(self, "ApiUrl", value=api_url)
