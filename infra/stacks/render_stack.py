"""Step Functions render pipeline + Remotion Lambda + assets bucket."""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_s3 as s3,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_dynamodb as ddb,
)
from constructs import Construct
import os
import sys

# Import RenderBudget from backend/guardrails.py as the single source of
# truth for all render resource limits.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
from guardrails import RenderBudget  # type: ignore

from .bundling import backend_code, node_code


class RenderStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        shared_code = backend_code()

        # DynamoDB single table: episodes, ideas, scripts, render metadata.
        # Lives in the storage tier alongside the assets bucket so both the API
        # Lambda (api_stack) and the pack Lambda (render_stack) can reference it.
        self.table = ddb.Table(
            self,
            "ProjectsTable",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.table.add_global_secondary_index(
            index_name="byType",
            partition_key=ddb.Attribute(name="gsi1pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="gsi1sk", type=ddb.AttributeType.STRING),
        )

        # Shared assets bucket: uploaded audio, transcripts, scripts, tts mp3s,
        # sliced audio, b-roll, final mp4s.
        self.assets_bucket = s3.Bucket(
            self,
            "AssetsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            cors=[
                # Browser uploads audio directly via presigned PUT.
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.GET, s3.HttpMethods.HEAD],
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                    exposed_headers=["ETag"],
                    max_age=3000,
                )
            ],
            lifecycle_rules=[
                s3.LifecycleRule(
                    prefix="tmp/",
                    expiration=Duration.days(7),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Lambdas for pipeline steps (source deployed via asset in production)
        common_env = {
            "ASSETS_BUCKET": self.assets_bucket.bucket_name,
            "TABLE_NAME": self.table.table_name,
        }

        # (FFmpeg layer moved into ApiStack — it's only used by the API Lambda now.)

        broll_fn = _lambda.Function(
            self,
            "BrollFn",
            runtime=_lambda.Runtime.PYTHON_3_11,
            architecture=_lambda.Architecture.ARM_64,
            handler="broll.handler",
            code=shared_code,
            # 15 min so Nova Reel fallback has room — Nova jobs take 3-5 min
            # each and we wait on up to 3 in parallel.
            timeout=Duration.minutes(15),
            memory_size=512,
            environment=common_env,
        )
        self.assets_bucket.grant_read_write(broll_fn)
        broll_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/vyas-video/*"
                ],
            )
        )
        broll_fn.add_to_role_policy(
            iam.PolicyStatement(
                # Nova Reel async text-to-video as Pexels fallback.
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:StartAsyncInvoke",
                    "bedrock:GetAsyncInvoke",
                ],
                resources=["*"],
            )
        )

        # Remotion render invoker (calls @remotion/lambda render function).
        # Remotion Lambda is deployed by `pnpm exec remotion lambda functions
        # deploy` using the resource limits defined in RenderBudget below.
        # The function name encodes those limits (mem{MB}mb-disk{MB}mb-{S}sec),
        # so we derive the expected name from RenderBudget.
        budget = RenderBudget()
        expected_fn_name = (
            f"remotion-render-4-0-220-"
            f"mem{budget.remotion_lambda_memory_mb}mb-"
            f"disk{budget.remotion_lambda_disk_mb}mb-"
            f"{budget.remotion_lambda_timeout_sec}sec"
        )
        remotion_fn_name = self.node.try_get_context("remotionFunctionName") or expected_fn_name
        remotion_serve_url = self.node.try_get_context("remotionServeUrl") or (
            "https://remotionlambda-REPLACE_WITH_YOUR_BUCKET.s3.us-east-1.amazonaws.com/sites/vyas-video/index.html"
        )

        # Node Lambda that uses @remotion/lambda SDK — raw boto3 invocation
        # is rejected by Remotion's version handshake.
        render_fn = _lambda.Function(
            self,
            "RenderInvokerFn",
            runtime=_lambda.Runtime.NODEJS_20_X,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=node_code("remotion-invoker"),
            timeout=Duration.minutes(15),
            memory_size=1024,
            environment={
                **common_env,
                "REMOTION_FUNCTION_NAME": remotion_fn_name,
                "REMOTION_SERVE_URL": remotion_serve_url,
                # Render budget knobs sourced from backend/guardrails.py.
                # Changing the numbers there updates both CDK + invoker.
                "FRAMES_PER_CHUNK": str(budget.frames_per_chunk),
                "INVOKER_POLL_DEADLINE_SEC": str(budget.invoker_poll_deadline_sec),
                "INPUT_PROPS_MAX_BYTES": str(budget.input_props_max_bytes),
            },
        )
        render_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:remotion-render-*"],
            )
        )
        # Read the Remotion site bundle from the Remotion-owned S3 bucket
        render_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    "arn:aws:s3:::remotionlambda-*",
                    "arn:aws:s3:::remotionlambda-*/*",
                ],
            )
        )
        self.assets_bucket.grant_read_write(render_fn)

        pack_fn = _lambda.Function(
            self,
            "PackFn",
            runtime=_lambda.Runtime.PYTHON_3_11,
            architecture=_lambda.Architecture.ARM_64,
            handler="pack.handler",
            code=shared_code,
            timeout=Duration.minutes(2),
            memory_size=256,
            environment=common_env,
        )
        self.assets_bucket.grant_read_write(pack_fn)
        self.table.grant_read_write_data(pack_fn)

        # Step Functions: tts -> broll -> render -> pack
        # Render pipeline: Broll → Remotion → Pack.
        # (AudioSlice used to run here but now happens inline during POST /script
        # so the user can preview the sliced clips before paying for a render.)
        definition = (
            tasks.LambdaInvoke(self, "Broll", lambda_function=broll_fn, output_path="$.Payload")
            .next(tasks.LambdaInvoke(self, "Render", lambda_function=render_fn, output_path="$.Payload"))
            .next(tasks.LambdaInvoke(self, "Pack", lambda_function=pack_fn, output_path="$.Payload"))
        )

        self.state_machine = sfn.StateMachine(
            self,
            "RenderPipeline",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.minutes(30),
        )

        CfnOutput(self, "AssetsBucketName", value=self.assets_bucket.bucket_name)
        CfnOutput(self, "StateMachineArn", value=self.state_machine.state_machine_arn)
