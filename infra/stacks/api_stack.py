"""API Gateway HTTP API + Lambda handlers + DynamoDB single table."""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_lambda as _lambda,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as apigw_auth,
    aws_apigatewayv2_integrations as integrations,
    aws_cognito as cognito,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_s3 as s3,
    aws_stepfunctions as sfn,
)
from constructs import Construct

from .bundling import backend_code, ffmpeg_layer_code


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        assets_bucket: s3.IBucket,
        render_state_machine: sfn.IStateMachine,
        table: ddb.ITable,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # FFmpeg layer — ARM64 static binary at /opt/bin/ffmpeg. Used inline
        # by this Lambda when generating scripts to slice verbatim audio clips.
        ffmpeg_layer = _lambda.LayerVersion(
            self,
            "FfmpegLayer",
            code=ffmpeg_layer_code(),
            compatible_architectures=[_lambda.Architecture.ARM_64],
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_11],
            description="Static ffmpeg + ffprobe (ARM64)",
        )

        env = {
            "TABLE_NAME": table.table_name,
            "ASSETS_BUCKET": assets_bucket.bucket_name,
            "STATE_MACHINE_ARN": render_state_machine.state_machine_arn,
            # Cross-region inference profiles (required — on-demand throughput not
            # supported for Opus/Sonnet 4.6 direct model IDs).
            "BEDROCK_IDEATION_MODEL": "us.anthropic.claude-opus-4-6-v1",
            "BEDROCK_SCRIPT_MODEL": "us.anthropic.claude-sonnet-4-6",
        }

        api_fn = _lambda.Function(
            self,
            "ApiFn",
            runtime=_lambda.Runtime.PYTHON_3_11,
            architecture=_lambda.Architecture.ARM_64,
            handler="api.handler",
            code=backend_code(),
            timeout=Duration.minutes(5),  # multi-shot: screenwriter ~130s + director ~70s + slice ~15s
            memory_size=2048,  # FFmpeg slicing + Bedrock streaming runs inline here now
            environment=env,
            layers=[ffmpeg_layer],
        )
        table.grant_read_write_data(api_fn)
        assets_bucket.grant_read_write(api_fn)
        render_state_machine.grant_start_execution(api_fn)
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=["*"],
            )
        )
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                # Start + poll async Transcribe jobs on uploaded podcast audio.
                actions=[
                    "transcribe:StartTranscriptionJob",
                    "transcribe:GetTranscriptionJob",
                    "transcribe:DeleteTranscriptionJob",
                ],
                resources=["*"],
            )
        )
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                # Bug-guard fallback when a scene lacks source timestamps.
                actions=["polly:SynthesizeSpeech"],
                resources=["*"],
            )
        )
        # Self-invoke (async) so long-running ideation can run in the background
        # without tripping the API Gateway HTTP 30s integration timeout.
        # The Lambda learns its own function name at runtime via
        # `context.invoked_function_arn` — avoids a self-Ref CF cycle.
        api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=["*"],
            )
        )

        http_api = apigw.HttpApi(
            self,
            "HttpApi",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_methods=[apigw.CorsHttpMethod.ANY],
                allow_origins=["*"],
                allow_headers=["*", "Authorization"],
            ),
        )

        # Cognito JWT authorizer — validates ID tokens issued by our pool.
        jwt_authorizer = apigw_auth.HttpUserPoolAuthorizer(
            "CognitoAuthorizer",
            user_pool,
            user_pool_clients=[user_pool_client],
        )

        lambda_int = integrations.HttpLambdaIntegration("ApiInt", api_fn)

        # Public route: health check (no auth required).
        http_api.add_routes(
            path="/health",
            methods=[apigw.HttpMethod.GET],
            integration=lambda_int,
        )
        # Protected routes — only non-OPTIONS methods pass through the JWT
        # authorizer. OPTIONS (CORS preflight) is handled by API Gateway's
        # corsPreflight config automatically and must NOT hit the authorizer
        # (the browser doesn't send Authorization on preflight).
        http_api.add_routes(
            path="/{proxy+}",
            methods=[
                apigw.HttpMethod.GET,
                apigw.HttpMethod.POST,
                apigw.HttpMethod.PUT,
                apigw.HttpMethod.DELETE,
                apigw.HttpMethod.PATCH,
            ],
            integration=lambda_int,
            authorizer=jwt_authorizer,
        )

        self.api_url = http_api.api_endpoint
        CfnOutput(self, "ApiEndpoint", value=self.api_url)
        CfnOutput(self, "TableName", value=table.table_name)
