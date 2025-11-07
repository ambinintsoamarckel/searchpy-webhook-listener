# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2024-01-XX

### Added
- Initial release
- Automatic failure detection via webhooks
- Progressive remediation strategy
- Persistent state management
- HMAC authentication
- Log archiving before remediation
- Cooldown protection
- Discord notifications with embeds
- Health and status API endpoints

### Security
- HMAC token authentication on webhooks
- Non-root user in Docker container
- Read-only mounts where **possible**
