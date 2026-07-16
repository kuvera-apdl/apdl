"""Module entry point for trusted Codegen repository-grant revocation."""

from app.github.grant_cli import revoke_main


if __name__ == "__main__":
    revoke_main()
