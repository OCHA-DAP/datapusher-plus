# encoding: utf-8
from __future__ import annotations

from ckan.common import CKANConfig
import logging
from typing import Any, Callable

import ckan.model as model
import ckan.plugins as p
import ckanext.datapusher_plus.views as views
import ckanext.datapusher_plus.helpers as dph
import ckanext.datapusher_plus.logic.action as action
import ckanext.datapusher_plus.logic.auth as auth
import ckanext.datapusher_plus.cli as cli

tk = p.toolkit

log = logging.getLogger(__name__)

try:
    config_declarations = tk.blanket.config_declarations
except AttributeError:
    # CKAN 2.9 does not have config_declarations.
    # Remove when dropping support.
    def config_declarations(cls):
        return cls


# Get ready for CKAN 2.10 upgrade
if tk.check_ckan_version("2.10"):
    from ckan.types import Action, AuthFunction, Context


class DatastoreException(Exception):
    pass


@config_declarations
class DatapusherPlusPlugin(p.SingletonPlugin):
    p.implements(p.IConfigurer, inherit=True)
    p.implements(p.IConfigurable, inherit=True)
    p.implements(p.IActions)
    p.implements(p.IAuthFunctions)
    p.implements(p.IResourceUrlChange)
    p.implements(p.IResourceController, inherit=True)
    p.implements(p.ITemplateHelpers)
    p.implements(p.IBlueprint)
    p.implements(p.IClick)

    legacy_mode = False
    resource_show_action = None

    def update_config(self, config: CKANConfig):
        tk.add_template_directory(config, "templates")
        tk.add_public_directory(config, "public")
        tk.add_resource("assets", "datapusher_plus")

    # IResourceUrlChange
    def notify(self, resource: model.Resource):
        # Added by HDX: intentionally NOT calling self._submit_to_datapusher(resource_dict)
        # here anymore. This hook is dispatched by CKAN core from
        # DomainObjectModificationExtension.before_commit() (ckan/model/modification.py),
        # i.e. DURING the caller's transaction commit, BEFORE the data is actually durably
        # committed - and core provides NO exception guard around this specific dispatch
        # (unlike the IDomainObjectModification dispatch a few lines below it in the same
        # core file). That combination means submitting from here could (a) hand DataPusher
        # Plus a resource that isn't really persisted yet if the transaction later fails for
        # an unrelated reason, and (b) any failure here (e.g. resource_show or an allowlist
        # check raising) would aborts the caller's whole commit with no way to recover.
        #
        # ckanext-hdx_package's package_update() already covers this same scenario (an
        # existing resource's url changing without a real file upload) via its own
        # existing_resource_urls tracking, which flags such resources into
        # context[FILE_WAS_UPLOADED] and submits them through _manage_datastore_for_uploads()
        # AFTER the commit has fully succeeded, wrapped in a fail-open try/except. Keeping
        # this hook active would submit the same resource a second time (and reintroduce the
        # pre-commit/unguarded issue this comment describes).
        pass

    # IResourceController

    def after_resource_create(self, context, resource_dict: dict[str, Any]):
        # Added by HDX: intentionally NOT calling self._submit_to_datapusher(resource_dict) here.
        # New resources created via ckanext-hdx_package's resource_create() are already handled
        # there directly (ckanext-hdx_package/ckanext/hdx_package/actions/update.py /
        # ckanext-hdx_package/ckanext/hdx_package/actions/create.py):
        #  - Genuine file uploads are submitted via _manage_datastore_for_uploads(), invoked as
        #    part of the underlying package_revise -> package_update call chain for that action.
        #  - URL-only resources (no uploaded file) are submitted by resource_create() itself,
        #    right after creation, since package_update()'s upload-flagging logic never
        #    considers them.
        # Keeping this hook active for either case would submit the same brand-new resource to
        # DataPusher Plus twice.
        pass

    if not tk.check_ckan_version("2.10"):

        def after_create(self, context, resource_dict):
            self.after_resource_create(context, resource_dict)

    def _submit_to_datapusher(self, resource_dict: dict[str, Any]):
        log.info(f'Starting _submit_to_datapusher for resource id: {resource_dict.get("id")}')
        context = {"model": model, "ignore_auth": True, "defer_commit": True}

        resource_format = resource_dict.get("format")
        supported_formats = tk.config.get(
            "ckan.datapusher.formats") or tk.config.get(
                "ckanext.datapusher_plus.formats"
        )
        if not supported_formats:
            log.debug(
                "No supported formats configured,\
                    using DataPusher Plus internals")
            supported_formats = ["csv", "xls", "xlsx", "tsv"]

        submit = (
            resource_format
            and resource_format.lower() in supported_formats
            and resource_dict.get("url_type") != "datapusher"
        )

        # Added by HDX
        hdx_allowed = p.toolkit.get_action('hdx_is_package_allowed_for_datastore')(
            {}, {'package_id': resource_dict['package_id']}
        )
        if not hdx_allowed:
            log.info(f'Package {resource_dict["package_id"]} not allowed for datastore, so not submitting resource {resource_dict["id"]} to DataPusher Plus')
        submit = submit and hdx_allowed
        # END - Added by HDX

        if not submit:
            log.info(f'Not submitting resource {resource_dict["id"]} to DataPusher Plus ')
            return

        try:
            task = tk.get_action("task_status_show")(
                context,
                {
                    "entity_id": resource_dict["id"],
                    "task_type": "datapusher_plus",
                    "key": "datapusher_plus",
                },
            )

            if task.get("state") in ("pending", "submitting", "running"):
                # There already is a pending DataPusher submission,
                # skip this one ...
                log.info(
                    "Skipping DataPusher Plus submission for "
                    "resource {0}".format(resource_dict["id"])
                )
                return
        except tk.ObjectNotFound:
            pass

        try:
            log.info(
                "Submitting resource {0}".format(resource_dict["id"])
                + " to DataPusher Plus"
            )
            tk.get_action("datapusher_submit")(
                context, {"resource_id": resource_dict["id"]}
            )
        except tk.ValidationError as e:
            # If datapusher is offline want to catch error instead
            # of raising otherwise resource save will fail with 500
            log.critical(e)
            pass

    def get_actions(self) -> dict[str, Action]:
        return {
            "datapusher_submit": action.datapusher_submit,
            "datapusher_hook": action.datapusher_hook,
            "datapusher_status": action.datapusher_status,
        }

    def get_auth_functions(self) -> dict[str, AuthFunction]:
        return {
            "datapusher_submit": auth.datapusher_submit,
            "datapusher_status": auth.datapusher_status,
        }

    def get_helpers(self) -> dict[str, Callable[..., Any]]:
        return {
            "datapusher_plus_status": dph.datapusher_status,
            "datapusher_plus_status_description": dph.datapusher_status_description,
        }

    # IBlueprint

    def get_blueprint(self):
        return views.get_blueprints()

    # IClick
    def get_commands(self):
        return cli.get_commands()
