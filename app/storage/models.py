import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, DateTime, Text, ForeignKey, JSON
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entra_id = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    timezone = Column(String, default="UTC")
    teams_user_id = Column(String)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    linked_identities = relationship("LinkedIdentity", back_populates="profile", cascade="all, delete-orphan")
    integrations = relationship("Integration", back_populates="profile", cascade="all, delete-orphan")
    summaries = relationship("Summary", back_populates="profile", cascade="all, delete-orphan")
    query_logs = relationship("QueryLog", back_populates="profile", cascade="all, delete-orphan")


class LinkedIdentity(Base):
    __tablename__ = "linked_identities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    provider = Column(String, nullable=False)          # entra, github, gitlab, jira
    tenant_id = Column(String)
    workspace_label = Column(String)

    profile = relationship("Profile", back_populates="linked_identities")


class Integration(Base):
    __tablename__ = "integrations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    source = Column(String, nullable=False)            # teams_subscription | github | gitlab | jira
    access_token_enc = Column(Text)
    refresh_token_enc = Column(Text)
    token_expires_at = Column(DateTime(timezone=True))
    sync_status = Column(String, default="active")     # active | error
    last_synced_at = Column(DateTime(timezone=True))
    workspace = Column(String)

    # Teams Graph subscription
    subscription_id = Column(String)
    subscription_expires_at = Column(DateTime(timezone=True))

    # Jira
    jira_webhook_id = Column(String)
    jira_webhook_expires_at = Column(DateTime(timezone=True))

    # GitHub
    github_hook_id = Column(String)

    profile = relationship("Profile", back_populates="integrations")


class Summary(Base):
    __tablename__ = "summaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id = Column(UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=False)
    period_type = Column(String, nullable=False)       # daily | weekly
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    content = Column(Text, nullable=False)
    workspace = Column(String)
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
