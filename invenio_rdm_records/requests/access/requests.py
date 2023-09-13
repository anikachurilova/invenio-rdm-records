# -*- coding: utf-8 -*-
#
# Copyright (C) 2023 TU Wien.
#
# Invenio-RDM-Records is free software; you can redistribute it and/or modify
# it under the terms of the MIT License; see LICENSE file for more details.

"""Access requests for records."""
from datetime import datetime, timedelta

import marshmallow as ma
from flask import current_app, g
from invenio_access.permissions import authenticated_user, system_identity
from invenio_drafts_resources.services.records.uow import ParentRecordCommitOp
from invenio_i18n import lazy_gettext as _
from invenio_mail.tasks import send_email
from invenio_notifications.services.uow import NotificationOp
from invenio_records_resources.services.uow import Operation
from invenio_requests import current_events_service
from invenio_requests.customizations import RequestType, actions
from invenio_requests.customizations.event_types import CommentEventType
from marshmallow import ValidationError, fields, validates
from marshmallow_utils.permissions import FieldPermissionsMixin

from invenio_rdm_records.notifications.builders import (
    GuestAccessRequestAcceptNotificationBuilder,
    UserAccessRequestAcceptNotificationBuilder,
)

from ...proxies import current_rdm_records_service as service


class EmailOp(Operation):
    """A notification operation."""

    def __init__(self, receiver, subject, html_body, body):
        """Initialize operation."""
        self.receiver = receiver
        self.subject = subject
        self.html_body = html_body
        self.body = body

    def on_post_commit(self, uow):
        """Start task to send text via email."""
        send_email(
            {
                "subject": self.subject,
                "html_body": self.html_body,
                "body": self.body,
                "recipients": [self.receiver],
                "sender": current_app.config["MAIL_DEFAULT_SENDER"],
            }
        )


#
# Actions
#
class UserSubmitAction(actions.SubmitAction):
    """Submit action for user access requests."""

    def execute(self, identity, uow):
        """Execute the submit action."""
        self.request["title"] = self.request.topic.resolve().metadata["title"]
        super().execute(identity, uow)


class GuestSubmitAction(actions.SubmitAction):
    """Submit action for guest access requests."""

    def execute(self, identity, uow):
        """Execute the submit action."""
        record = self.request.topic.resolve()
        self.request["title"] = record.metadata["title"]

        super().execute(identity, uow)


class GuestAcceptAction(actions.AcceptAction):
    """Accept action."""

    def execute(self, identity, uow):
        """Accept guest access request."""
        record = service.read(
            id_=self.request.topic.reference_dict["record"], identity=system_identity
        )
        payload = self.request["payload"]

        # NOTE: the description isn't translated because it can be changed later
        #       by the record owner
        data = {
            "permission": payload["permission"],
            "description": (
                f"Requested by guest: {payload['full_name']} ({payload['email']})"
            ),
            "origin": f"request:{self.request.id}",
        }

        # secret link will never expire if secret_link_expiration is empty
        days = int(payload["secret_link_expiration"])
        # TODO date calculation could be done elsewhere ?
        if days:
            data["expires_at"] = (
                (datetime.utcnow() + timedelta(days=days)).date().isoformat()
            )
        link = service.access.create_secret_link(identity, record.id, data)
        access_url = f"{record.links['self_html']}?token={link._link.token}"

        uow.register(
            ParentRecordCommitOp(
                record._record.parent, indexer_context=dict(service=service)
            )
        )
        uow.register(
            NotificationOp(
                GuestAccessRequestAcceptNotificationBuilder.build(
                    self.request, access_url=access_url
                )
            )
        )

        super().execute(identity, uow)

        confirmation_message = {
            "payload": {
                "content": 'Click <a href="{url}">here</a> to access the record.'.format(
                    url=access_url
                )
            }
        }
        current_events_service.create(
            system_identity,
            self.request.id,
            confirmation_message,
            CommentEventType,
            uow=uow,
            notify=False,
        )


class UserAcceptAction(actions.AcceptAction):
    """Accept action."""

    def execute(self, identity, uow):
        """Accept user access request."""
        creator = self.request.created_by.resolve()
        record = self.request.topic.resolve()
        permission = self.request["payload"]["permission"]

        data = {
            "permission": permission,
            "subject": {
                "type": "user",
                "id": str(creator.id),
            },
            "origin": f"request:{self.request.id}",
        }

        # NOTE: we're using the system identity here to avoid the grant creation
        #       potentially being blocked by the requesting user's profile visibility
        service.access.create_grant(system_identity, record.pid.pid_value, data)
        uow.register(
            ParentRecordCommitOp(record.parent, indexer_context=dict(service=service))
        )
        uow.register(
            NotificationOp(
                UserAccessRequestAcceptNotificationBuilder.build(self.request)
            )
        )

        super().execute(identity, uow)


#
# Requests
#
class UserAccessRequest(RequestType):
    """Access request type coming from a user."""

    type_id = "user-access-request"
    name = _("Access request")

    creator_can_be_none = False
    topic_can_be_none = False
    allowed_creator_ref_types = ["user"]
    allowed_receiver_ref_types = ["user", "community"]
    allowed_topic_ref_types = ["record"]

    def _update_link_config(self, **context_vars):
        """Update the 'ui' variable for generation of links."""
        return {"ui": context_vars["ui"] + "/access"}

    available_actions = {
        "create": actions.CreateAction,
        "submit": UserSubmitAction,
        "delete": actions.DeleteAction,
        "accept": UserAcceptAction,
        "cancel": actions.CancelAction,
        "decline": actions.DeclineAction,
        "expire": actions.ExpireAction,
    }

    payload_schema = {
        "permission": fields.String(required=True),
        "message": fields.String(required=False),
    }


class GuestAccessRequest(RequestType):
    """Access request type coming from a guest."""

    type_id = "guest-access-request"
    name = _("Access request")

    creator_can_be_none = False
    topic_can_be_none = False
    allowed_creator_ref_types = ["email"]
    allowed_receiver_ref_types = ["user", "community"]
    allowed_topic_ref_types = ["record"]

    @classmethod
    def _create_payload_cls(cls):
        class PayloadBaseSchema(ma.Schema, FieldPermissionsMixin):
            field_load_permissions = {
                "secret_link_expiration": "manage_access_options",
            }

            class Meta:
                unknown = ma.RAISE

        cls.payload_schema_cls = PayloadBaseSchema

    def _update_link_config(self, **context_vars):
        """Fix the prefix required for "self_html"."""
        prefix = "/me"

        if hasattr(g, "identity"):
            identity = context_vars.get("identity", g.identity)

            if authenticated_user not in identity.provides:
                prefix = "/access"

        return {"ui": context_vars["ui"] + prefix}

    @validates("secret_link_expiration")
    def _validate_days(self, value):
        try:
            if int(value) < 0:
                raise ValidationError(
                    message="Not a valid number of days.",
                    field_name="secret_link_expiration",
                )
        except ValueError:
            raise ValidationError(
                message="Not a valid number of days.",
                field_name="secret_link_expiration",
            )

    available_actions = {
        "create": actions.CreateAction,
        "submit": GuestSubmitAction,
        "delete": actions.DeleteAction,
        "accept": GuestAcceptAction,
        "cancel": actions.CancelAction,
        "decline": actions.DeclineAction,
        "expire": actions.ExpireAction,
    }

    payload_schema = {
        "permission": fields.String(required=True),
        "email": fields.Email(required=True),
        "full_name": fields.String(required=True),
        "token": fields.String(required=True),
        "message": fields.String(required=True),
        "secret_link_expiration": fields.String(required=True),
        "consent_to_share_personal_data": fields.String(required=True),
    }
