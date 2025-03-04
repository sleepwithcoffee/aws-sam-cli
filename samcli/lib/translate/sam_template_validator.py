"""
Library for Validating Sam Templates
"""
import functools
import logging
from typing import Dict, cast

from boto3.session import Session
from samtranslator.parser import parser
from samtranslator.public.exceptions import InvalidDocumentException
from samtranslator.translator.managed_policy_translator import ManagedPolicyLoader
from samtranslator.translator.translator import Translator

from samcli.commands.validate.lib.exceptions import InvalidSamDocumentException
from samcli.lib.utils.packagetype import IMAGE, ZIP
from samcli.lib.utils.resources import AWS_SERVERLESS_FUNCTION
from samcli.yamlhelper import yaml_dump

LOG = logging.getLogger(__name__)


class SamTemplateValidator:
    def __init__(self, sam_template, managed_policy_loader: ManagedPolicyLoader, profile=None, region=None):
        """
        Construct a SamTemplateValidator

        Design Details:

        managed_policy_loader is injected into the `__init__` to allow future expansion
        and overriding capabilities. A typically pattern is to pass the name of the class into
        the `__init__` as keyword args. As long as the class 'conforms' to the same 'interface'.
        This allows the class to be changed by the client and allowing customization of the class being
        initialized. Something I had in mind would be allowing a template to be run and checked
        'offline' (not needing aws creds). To make this an easier transition in the future, we ingest
        the ManagedPolicyLoader class.

        Parameters
        ----------
        sam_template dict
            Dictionary representing a SAM Template
        managed_policy_loader ManagedPolicyLoader
            Sam ManagedPolicyLoader
        """
        self.sam_template = sam_template
        self.managed_policy_loader = managed_policy_loader
        self.sam_parser = parser.Parser()
        self.boto3_session = Session(profile_name=profile, region_name=region)

    def get_translated_template_if_valid(self):
        """
        Runs the SAM Translator to determine if the template provided is valid. This is similar to running a
        ChangeSet in CloudFormation for a SAM Template

        Raises
        -------
        InvalidSamDocumentException
             If the template is not valid, an InvalidSamDocumentException is raised
        """

        sam_translator = Translator(
            managed_policy_map=None,
            sam_parser=self.sam_parser,
            plugins=[],
            boto_session=self.boto3_session,
        )

        self._replace_local_codeuri()
        self._replace_local_image()

        try:
            template = sam_translator.translate(
                sam_template=self.sam_template, parameter_values={}, get_managed_policy_map=self._get_managed_policy_map
            )
            LOG.debug("Translated template is:\n%s", yaml_dump(template))
            return yaml_dump(template)
        except InvalidDocumentException as e:
            raise InvalidSamDocumentException(
                functools.reduce(lambda message, error: message + " " + str(error), e.causes, str(e))
            ) from e

    @functools.lru_cache(maxsize=None)
    def _get_managed_policy_map(self) -> Dict[str, str]:
        """
        Helper function for getting managed policies and caching them.
        Used by the transform for loading policies.

        Returns
        -------
        Dict[str, str]
            Dictionary containing the policy map
        """
        return cast(Dict[str, str], self.managed_policy_loader.load())

    def _replace_local_codeuri(self):
        """
        Replaces the CodeUri in AWS::Serverless::Function and DefinitionUri in AWS::Serverless::Api and
        AWS::Serverless::HttpApi to a fake S3 Uri. This is to support running the SAM Translator with
        valid values for these fields. If this in not done, the template is invalid in the eyes of SAM
        Translator (the translator does not support local paths)
        """

        all_resources = self.sam_template.get("Resources", {})
        global_settings = self.sam_template.get("Globals", {})

        for resource_type, properties in global_settings.items():
            if resource_type == "Function":
                if all(
                    [
                        _properties.get("Properties", {}).get("PackageType", ZIP) == ZIP
                        for _, _properties in all_resources.items()
                    ]
                    + [_properties.get("PackageType", ZIP) == ZIP for _, _properties in global_settings.items()]
                ):
                    SamTemplateValidator._update_to_s3_uri("CodeUri", properties)

        for _, resource in all_resources.items():
            resource_type = resource.get("Type")
            resource_dict = resource.get("Properties", {})

            if resource_type == "AWS::Serverless::Function" and resource_dict.get("PackageType", ZIP) == ZIP:
                SamTemplateValidator._update_to_s3_uri("CodeUri", resource_dict)

            if resource_type == "AWS::Serverless::LayerVersion":
                SamTemplateValidator._update_to_s3_uri("ContentUri", resource_dict)

            if resource_type == "AWS::Serverless::Api":
                if "DefinitionUri" in resource_dict:
                    SamTemplateValidator._update_to_s3_uri("DefinitionUri", resource_dict)

            if resource_type == "AWS::Serverless::HttpApi":
                if "DefinitionUri" in resource_dict:
                    SamTemplateValidator._update_to_s3_uri("DefinitionUri", resource_dict)

            if resource_type == "AWS::Serverless::StateMachine":
                if "DefinitionUri" in resource_dict:
                    SamTemplateValidator._update_to_s3_uri("DefinitionUri", resource_dict)

    def _replace_local_image(self):
        """
        Adds fake ImageUri to AWS::Serverless::Functions that reference a local image using Metadata.
        This ensures sam validate works without having to package the app or use ImageUri.
        """
        resources = self.sam_template.get("Resources", {})
        for _, resource in resources.items():
            resource_type = resource.get("Type")
            properties = resource.get("Properties", {})

            is_image_function = resource_type == AWS_SERVERLESS_FUNCTION and properties.get("PackageType") == IMAGE
            is_local_image = resource.get("Metadata", {}).get("Dockerfile")

            if is_image_function and is_local_image:
                if "ImageUri" not in properties:
                    properties["ImageUri"] = "111111111111.dkr.ecr.region.amazonaws.com/repository"

    @staticmethod
    def is_s3_uri(uri):
        """
        Checks the uri and determines if it is a valid S3 Uri

        Parameters
        ----------
        uri str, required
            Uri to check

        Returns
        -------
        bool
            Returns True if the uri given is an S3 uri, otherwise False

        """
        return isinstance(uri, str) and uri.startswith("s3://")

    @staticmethod
    def _update_to_s3_uri(property_key, resource_property_dict, s3_uri_value="s3://bucket/value"):
        """
        Updates the 'property_key' in the 'resource_property_dict' to the value of 's3_uri_value'

        Note: The function will mutate the resource_property_dict that is pass in

        Parameters
        ----------
        property_key str, required
            Key in the resource_property_dict
        resource_property_dict dict, required
            Property dictionary of a Resource in the template to replace
        s3_uri_value str, optional
            Value to update the value of the property_key to
        """
        uri_property = resource_property_dict.get(property_key, ".")

        # ignore if dict or already an S3 Uri
        if isinstance(uri_property, dict) or SamTemplateValidator.is_s3_uri(uri_property):
            return

        resource_property_dict[property_key] = s3_uri_value
