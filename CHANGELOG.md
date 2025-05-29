# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.7] - 2025-05-29

### Fixed
- Fixed JSONField filtering error that was causing AssertionError with django-filter
- JSONFields are now properly excluded from automatic filtering
- Added support for multiple JSONField implementations (standard Django and PostgreSQL)

### Added
- Enhanced field type detection with multiple safety checks
- Added proper handling for UUID and GenericIPAddress field types
- Improved robustness of field filtering logic
- Added "Known Limitations" section to README documenting JSONField filtering limitation
- Added comprehensive test suite for JSONField support

## [0.1.6] - 2025-05-29

### Fixed
- Add contents write permission for GitHub release assets
- Update CI/CD workflow for OIDC publishing

## [0.1.5] - 2025-05-29

### Fixed
- Update docs workflow actions to latest versions
- Fixed import error when Django not configured
- Fixed hardcoded version in test

### Changed
- Updated GitHub Actions from v3 to v4
- Applied code formatting (black, isort)

## [0.1.4] - 2025-05-29

### Fixed
- Fixed lazy imports to prevent Django configuration errors
- Fixed all linting issues

## [0.1.3] - 2025-05-29

### Fixed
- Fixed import issues with lazy loading

## [0.1.2] - 2025-05-29

### Fixed
- Fixed version test to be dynamic

## [0.1.1] - 2025-05-29

### Fixed
- Fixed JSONField filtering issue

## [0.1.0] - 2025-05-29

### Added
- Initial release of TurboDRF
- Zero-configuration Django REST API generation
- Role-based permissions system
- Automatic serializer generation
- Field-level permissions
- Built-in filtering, searching, and ordering
- Swagger/ReDoc API documentation
- Custom pagination with metadata
- Query optimization with select_related
- Support for nested field notation
- Comprehensive test suite

[0.1.7]: https://github.com/alexandercollins/turbodrf/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/alexandercollins/turbodrf/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/alexandercollins/turbodrf/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/alexandercollins/turbodrf/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/alexandercollins/turbodrf/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/alexandercollins/turbodrf/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/alexandercollins/turbodrf/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/alexandercollins/turbodrf/releases/tag/v0.1.0