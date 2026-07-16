# APDL Governance

APDL is an MIT-licensed, maintainer-led open source project. The current
maintainer roster is reflected in [CODEOWNERS](.github/CODEOWNERS).

## Roles

- **Contributors** participate in issues, documentation, testing, design, and
  code changes through pull requests.
- **Reviewers** are trusted contributors whom maintainers ask to review an area.
  Review authority does not by itself grant release or repository permissions.
- **Maintainers** triage reports, define supported scope, review and merge
  changes, manage security reports, and cut releases.

Maintainers may add or remove maintainers by documented consensus in a pull
request that updates this file and CODEOWNERS. Repository access is governed by
the GitHub organization and may be removed immediately for security or conduct
reasons.

## Decisions

Routine decisions are made in the issue or pull request that carries the
change. Maintainers seek practical consensus, using the tested contracts,
security boundaries, maintenance cost, and release scope as the deciding
criteria. If consensus cannot be reached, the maintainers responsible for the
affected area make the final decision and record the rationale publicly.

Material changes to canonical schemas, authentication/tenant authority,
artifact publication, licensing, governance, or supported deployment scope
require maintainer approval. Strict contracts are preferred over aliases or
silent compatibility behavior; a required migration must be explicit and
tested.

Security fixes may be developed privately and merged without prior public
discussion. Embargoed details are published only after a coordinated fix.

## Changes and Releases

- Pull requests should be focused, tested, and reviewed by a CODEOWNER or an
  explicitly delegated reviewer. Authors should not be the sole approver of
  their own material changes.
- CI is the merge and release gate. Failing or bypassed required checks must be
  resolved or documented by a maintainer before merge.
- Maintainers cut releases from a tested revision and publish only the artifact
  set declared in [SUPPORT.md](SUPPORT.md). Release notes must distinguish
  supported behavior from previews, experiments, and future designs.

Participation is subject to the [Code of Conduct](CODE_OF_CONDUCT.md). Security
reports use [SECURITY.md](SECURITY.md); ordinary support uses
[SUPPORT.md](SUPPORT.md).
