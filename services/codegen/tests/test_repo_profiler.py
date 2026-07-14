"""Fixture repositories for every Phase 1 ecosystem adapter."""

import json

import pytest
from pydantic import ValidationError

from app.profiling import profile_repository
from app.profiling.models import RepoProfile, UncertaintyCode


def _write(root, path: str, text: str = ""):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _codes(profile: RepoProfile) -> set[UncertaintyCode]:
    return {item.code for item in profile.uncertainties}


def test_node_workspace_profile_is_exact_and_stable(tmp_path):
    _write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "name": "web",
                "packageManager": "npm@10.8.0",
                "workspaces": ["apps/*"],
                "scripts": {
                    "lint": "eslint .",
                    "typecheck": "tsc --noEmit",
                    "build": "next build",
                    "test": "vitest run",
                },
                "dependencies": {"next": "^15.0.0"},
                "devDependencies": {"vitest": "^2.0.0", "@playwright/test": "^1.50.0"},
            }
        ),
    )
    _write(tmp_path, "apps/store/package.json", '{"name":"store"}')
    _write(
        tmp_path,
        "package-lock.json",
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "web"},
                    "node_modules/next": {"version": "15.0.4"},
                    "node_modules/vitest": {"version": "2.1.9"},
                    "node_modules/@playwright/test": {"version": "1.50.1"},
                },
            }
        ),
    )
    _write(tmp_path, "app/dashboard/page.tsx", "export default function Page() {}")
    _write(tmp_path, ".github/workflows/ci.yml", "name: ci")
    _write(tmp_path, "AGENTS.md", "instructions")

    profile = profile_repository(tmp_path, repo="acme/web", branch="main")

    assert profile == profile_repository(tmp_path, repo="acme/web", branch="main")
    assert profile.languages == ["JavaScript", "TypeScript"]
    assert "Next.js" in profile.frameworks
    assert profile.workspaces[0].members == ["apps/store"]
    assert (
        next(dep for dep in profile.dependencies if dep.name == "next").resolved_version
        == "15.0.4"
    )
    assert any(facility.browser for facility in profile.test_facilities)
    assert profile.routes[0].path == "app/dashboard/page.tsx"
    assert profile.ci_workflows[0].provider == "github_actions"
    assert profile.instructions[0].scope == "."
    assert profile.instructions[0].content == "instructions"


def test_conflicting_node_lockfiles_are_explicit(tmp_path):
    _write(tmp_path, "package.json", '{"dependencies":{"react":"^18"}}')
    _write(tmp_path, "package-lock.json", '{"lockfileVersion":3,"packages":{}}')
    _write(tmp_path, "pnpm-lock.yaml", "lockfileVersion: '9.0'")
    profile = profile_repository(tmp_path)
    assert UncertaintyCode.conflicting_package_managers in _codes(profile)
    assert {manager.name for manager in profile.package_managers} == {"npm", "pnpm"}


def test_python_uv_profile(tmp_path):
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname="api"\ndependencies=["fastapi>=0.115", "pytest>=8"]\n'
        "[tool.ruff]\nline-length=100\n[tool.pytest.ini_options]\n",
    )
    _write(
        tmp_path,
        "uv.lock",
        'version=1\n[[package]]\nname="fastapi"\nversion="0.115.6"\n'
        '[[package]]\nname="pytest"\nversion="8.3.4"\n',
    )
    _write(tmp_path, "app/main.py", "from fastapi import FastAPI\napp = FastAPI()")
    profile = profile_repository(tmp_path)
    assert profile.languages == ["Python"]
    assert "FastAPI" in profile.frameworks
    assert {command.kind.value for command in profile.commands} >= {
        "format",
        "lint",
        "test",
    }
    assert (
        next(
            dep for dep in profile.dependencies if dep.name == "fastapi"
        ).resolved_version
        == "0.115.6"
    )


def test_go_profile(tmp_path):
    _write(
        tmp_path,
        "go.mod",
        "module example.com/api\n\ngo 1.23\nrequire github.com/go-chi/chi v5.1.0\n",
    )
    _write(tmp_path, "go.sum", "github.com/go-chi/chi v5.1.0 h1:abc\n")
    _write(tmp_path, "cmd/api/main.go", "package main\nfunc main() {}")
    profile = profile_repository(tmp_path)
    assert profile.languages == ["Go"]
    assert profile.packages[0].name == "example.com/api"
    assert profile.dependencies[0].resolved_version == "v5.1.0"
    assert profile.entrypoints[0].kind == "go_main"


def test_rust_profile(tmp_path):
    _write(
        tmp_path,
        "Cargo.toml",
        '[package]\nname="worker"\nversion="0.1.0"\n[dependencies]\ntokio="1"\n',
    )
    _write(
        tmp_path,
        "Cargo.lock",
        'version=3\n[[package]]\nname="tokio"\nversion="1.42.0"\n',
    )
    _write(tmp_path, "src/main.rs", "fn main() {}")
    profile = profile_repository(tmp_path)
    assert profile.languages == ["Rust"]
    assert (
        next(
            dep for dep in profile.dependencies if dep.name == "tokio"
        ).resolved_version
        == "1.42.0"
    )
    assert any(
        command.command == "cargo clippy --all-targets --all-features -- -D warnings"
        for command in profile.commands
    )


def test_jvm_gradle_profile(tmp_path):
    _write(
        tmp_path,
        "build.gradle.kts",
        'dependencies { implementation("org.springframework:spring-core:6.2.0") }',
    )
    _write(
        tmp_path,
        "gradle.lockfile",
        "org.springframework:spring-core:6.2.1=runtimeClasspath\n",
    )
    _write(tmp_path, "gradlew", "#!/bin/sh")
    _write(tmp_path, "src/main/java/com/acme/Application.java", "class Application {}")
    profile = profile_repository(tmp_path)
    assert profile.languages == ["JVM"]
    assert "Spring" in profile.frameworks
    assert profile.dependencies[0].resolved_version == "6.2.1"
    assert profile.package_managers[0].name == "gradle_wrapper"


def test_jvm_maven_profile(tmp_path):
    _write(
        tmp_path,
        "pom.xml",
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<modelVersion>4.0.0</modelVersion><dependencies><dependency>"
        "<groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>"
        "<version>5.11.4</version></dependency></dependencies></project>",
    )
    _write(tmp_path, "mvnw", "#!/bin/sh")
    _write(tmp_path, "src/main/java/com/acme/Application.java", "class Application {}")
    profile = profile_repository(tmp_path)
    assert profile.package_managers[0].name == "maven"
    assert profile.dependencies[0].name == "org.junit.jupiter:junit-jupiter"
    assert profile.dependencies[0].resolved_version == "5.11.4"
    assert any(command.command == "./mvnw verify" for command in profile.commands)


def test_dotnet_profile(tmp_path):
    _write(
        tmp_path,
        "Api.csproj",
        '<Project Sdk="Microsoft.NET.Sdk.Web"><ItemGroup>'
        '<PackageReference Include="Serilog" Version="4.1.0" />'
        "</ItemGroup></Project>",
    )
    _write(
        tmp_path,
        "packages.lock.json",
        json.dumps(
            {
                "version": 1,
                "dependencies": {"net8.0": {"Serilog": {"resolved": "4.1.0"}}},
            }
        ),
    )
    _write(tmp_path, "Program.cs", "var app = WebApplication.CreateBuilder(args);")
    profile = profile_repository(tmp_path)
    assert profile.languages == ["C#"]
    assert profile.dependencies[0].resolved_version == "4.1.0"
    assert profile.entrypoints[0].kind == "dotnet_program"
    assert "packages.lock.json" in profile.protected_paths


def test_profile_schema_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RepoProfile.model_validate(
            {"schema_version": "repo_profile@1", "unexpected": True}
        )
