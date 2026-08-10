import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Index, Integer, String, DateTime, Text, ForeignKey, JSON, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


def utcnow():
    return datetime.now(timezone.utc)


DEFAULT_PERMISSIONS = ["email_report", "export_my_day", "export_analytics", "email_ai_answer"]

# The "Report" group. Base = counts of github/gitlab/jira. Everything else widens
# either the connector set or the depth, and each is off until an admin grants it.
# ADMIN_ONLY: never delegable by a manager and never held by a plain user — see
# rbac.eligible_for()/sanitize_permissions(), which are the only enforcement points.
ADMIN_ONLY_PERMISSIONS = ["teams_activity", "outlook_activity",
                          "activity_detail", "device_activity"]
REPORT_PERMISSIONS = ["consolidated_report", "attendance_report"]
ALL_PERMISSIONS = (DEFAULT_PERMISSIONS
                   + REPORT_PERMISSIONS
                   + ADMIN_ONLY_PERMISSIONS)

# Permissions only an ADMIN may hand out. Wider than ADMIN_ONLY: a plain user may
# HOLD the two report permissions, but a manager can never grant them — deciding
# who sees report data is an admin call. Enforced in rbac.assignable_permissions(),
# which every manager write path is clamped by.
NON_DELEGABLE_PERMISSIONS = REPORT_PERMISSIONS + ADMIN_ONLY_PERMISSIONS


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entra_id = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    timezone = Column(String, default="UTC")
    teams_user_id = Column(String)
    role = Column(String, nullable=False, default="user")
    permissions = Column(JSON, nullable=False, default=lambda: list(DEFAULT_PERMISSIONS))
    assignable_perms = Column(JSON, nullable=False, default=list)
    manager_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    manager = relationship("Profile", remote_side=[id], backref="reports", passive_deletes=True)

    linked_identities = relationship("LinkedIdentity", back_populates="profile", cascade="all, delete-orphan")
    integrations = relationship("Integration", back_populates="profile", cascade="all, delete-orphan")
    summaries = relationship("Summary", back_populates="profile", cascade="all, delete-orphan")
    query_logs = relationship("QueryLog", back_populates="profile", cascade="all, delete-orphan")
    chat_conversations = relationship("ChatConversation", back_populates="profile", cascade="all, delete-orphan")
    devices = relationship("Device", back_populates="profile", cascade="all, delete-orphan")
    email_preferences = relationship("EmailPreference", back_populates="profile", cascade="all, delete-orphan")


class LinkedIdentity(Base):
    __tablename__ = "linked_identities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    provider = Column(String, nullable=False)
    tenant_id = Column(String)
    workspace_label = Column(String)

    profile = relationship("Profile", back_populates="linked_identities")

    __table_args__ = (
        UniqueConstraint("profile_id", "provider", "workspace_label", name="uq_linked_identity_profile_provider_workspace"),
    )


class Integration(Base):
    __tablename__ = "integrations"
    __mapper_args__ = {
        "polymorphic_on": "source",
        "polymorphic_identity": None,
    }

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    source = Column(String, nullable=False)
    access_token_enc = Column(Text)
    refresh_token_enc = Column(Text)
    token_expires_at = Column(DateTime(timezone=True))
    sync_status = Column(String, default="active")
    last_synced_at = Column(DateTime(timezone=True))
    workspace = Column(String)

    subscription_id = Column(String)
    subscription_expires_at = Column(DateTime(timezone=True))

    jira_webhook_id = Column(String)
    jira_webhook_expires_at = Column(DateTime(timezone=True))

    github_hook_id = Column(String)

    profile = relationship("Profile", back_populates="integrations")

    __table_args__ = (
        Index("ix_integrations_source_sub_expires", "source", "subscription_expires_at"),
        Index("ix_integrations_source_jira_expires", "source", "jira_webhook_expires_at"),
    )


class TeamsIntegration(Integration):
    __mapper_args__ = {"polymorphic_identity": "teams_subscription"}


class GitHubIntegration(Integration):
    __mapper_args__ = {"polymorphic_identity": "github"}


class GitLabIntegration(Integration):
    __mapper_args__ = {"polymorphic_identity": "gitlab"}


class JiraIntegration(Integration):
    __mapper_args__ = {"polymorphic_identity": "jira"}


class Summary(Base):
    __tablename__ = "summaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    period_type = Column(String, nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    content = Column(Text, nullable=False)
    delivered_at = Column(DateTime(timezone=True))

    profile = relationship("Profile", back_populates="summaries")


class QueryLog(Base):
    __tablename__ = "query_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    question = Column(Text, nullable=False)
    filters_json = Column(JSON)
    ai_response = Column(Text)
    context_event_ids = Column(JSON)
    asked_at = Column(DateTime(timezone=True), default=utcnow)

    profile = relationship("Profile", back_populates="query_logs")


class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    title = Column(String(200), nullable=False, default="New chat")
    foundry_thread_id = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    profile = relationship("Profile", back_populates="chat_conversations")
    messages = relationship(
        "ChatMessage", back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("chat_conversations.id"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    conversation = relationship("ChatConversation", back_populates="messages")


class Device(Base):
    __tablename__ = "devices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    name = Column(String(100), nullable=False)
    platform = Column(String(20))
    last_seen = Column(DateTime(timezone=True))
    registered_at = Column(DateTime(timezone=True), default=utcnow)

    profile = relationship("Profile", back_populates="devices")
    tokens = relationship("DeviceToken", back_populates="device", cascade="all, delete-orphan")


class DeviceToken(Base):
    __tablename__ = "device_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    device = relationship("Device", back_populates="tokens")


class EmailPreference(Base):
    """A scheduled email digest: which report, how often, at what local hour."""
    __tablename__ = "email_preferences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    kind = Column(String, nullable=False)
    frequency = Column(String, nullable=False, default="daily")
    hour = Column(Integer, nullable=False, default=9)
    weekday = Column(Integer, nullable=False, default=4)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    profile = relationship("Profile", back_populates="email_preferences")

    __table_args__ = (
        UniqueConstraint("profile_id", "kind", name="uq_email_pref_profile_kind"),
    )
