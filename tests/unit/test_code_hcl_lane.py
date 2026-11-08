"""Terraform and terragrunt blocks are symbols named the
way their expressions spell them, references bind as `reads` edges, module
sources and terragrunt config paths resolve to the repository's files."""

from __future__ import annotations

from server.code.domain.extract.lane_units import bind_sites, span_chunks
from server.code.domain.extract.lang_detect import detect_by_extension
from server.code.domain.extract.lang_hcl import (
    CHUNK_OPTIONS,
    SITE_RULES,
    hcl_scope,
    parse_hcl,
    reference_sites,
    resolve_imports,
    wanted_paths,
)

MAIN = b"""# the network module
terraform {
  required_providers { aws = { source = "hashicorp/aws" } }
}

provider "aws" {
  region = var.region
}

locals {
  name   = "${var.prefix}-vpc"
  tags   = merge(var.tags, { Name = local.name })
}

resource "aws_vpc" "main" {
  cidr_block = var.cidr
  tags       = local.tags
}

resource "aws_subnet" "a" {
  vpc_id     = aws_vpc.main.id  # the vpc above
  cidr_block = cidrsubnet(var.cidr, 8, 0)
}

data "aws_availability_zones" "available" {}

module "kms" {
  source = "../aws-kms"
  alias  = local.name
}

output "vpc_id" {
  value = aws_vpc.main.id
}
"""

VARIABLES = b"""variable "region" {
  type    = string
  default = "us-east-2"
}

variable "cidr" { type = string }
variable "prefix" { type = string }
variable "tags" { type = map(string) }
"""

TERRAGRUNT = b"""include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../modules//aws-vpc"
}

dependency "kms" {
  config_path = "../kms"
}

inputs = {
  cidr    = "10.0.0.0/16"
  kms_key = dependency.kms.outputs.key_arn
}
"""


def test_blocks_are_symbols_named_like_their_references() -> None:
    pf = parse_hcl("infra/modules/aws-vpc/main.tf", MAIN)
    assert hcl_scope("infra/modules/aws-vpc/main.tf") == "infra.modules.aws-vpc"
    by = {s.name: s for s in pf.symbols}
    assert set(by) == {
        "provider.aws",
        "local.name",
        "local.tags",
        "aws_vpc.main",
        "aws_subnet.a",
        "data.aws_availability_zones.available",
        "module.kms",
        "output.vpc_id",
    }
    assert (
        by["aws_vpc.main"].kind == "resource"
        and by["aws_vpc.main"].signature == 'resource "aws_vpc"'
    )
    assert by["module.kms"].kind == "module" and 'source="../aws-kms"' in by["module.kms"].signature
    assert by["local.name"].kind == "const" and by["output.vpc_id"].kind == "output"
    assert by["aws_vpc.main"].qualified_name == "infra.modules.aws-vpc::aws_vpc.main"
    assert by["aws_vpc.main"].start_line == 15 and by["aws_vpc.main"].end_line == 18
    assert pf.includes == ["../aws-kms"], "the module source is the file's include specifier"
    vars_pf = parse_hcl("infra/modules/aws-vpc/variables.tf", VARIABLES)
    assert {s.name: s.signature for s in vars_pf.symbols} == {
        "var.region": "variable string",
        "var.cidr": "variable string",
        "var.prefix": "variable string",
        "var.tags": "variable map(string)",
    }


def test_references_bind_within_the_module_and_repo_wide() -> None:
    pf = parse_hcl("infra/modules/aws-vpc/main.tf", MAIN)
    vars_pf = parse_hcl("infra/modules/aws-vpc/variables.tf", VARIABLES)
    sites = reference_sites(pf)
    names = {n for _l, n, _k in sites}
    assert {
        "var.region",
        "var.cidr",
        "var.prefix",
        "var.tags",
        "local.name",
        "local.tags",
        "aws_vpc.main",
    } <= names
    assert "cidrsubnet" not in names and "merge" not in names, "functions are not references"
    table = {s.name: [(s.id, s.qualified_name, s.kind)] for s in [*pf.symbols, *vars_pf.symbols]}
    edges = bind_sites(pf, sites, table, SITE_RULES)
    got = {
        (e.from_qualified_name.split("::")[1], e.to_qualified_name.split("::")[1], e.confidence)
        for e in edges
    }
    assert ("aws_subnet.a", "aws_vpc.main", 1.0) in got, "same-file resource reference at 1.0"
    assert ("output.vpc_id", "aws_vpc.main", 1.0) in got
    assert ("local.tags", "local.name", 1.0) in got
    assert ("aws_vpc.main", "var.cidr", 0.6) in got, "a variable in another file of the module"
    assert all(e.edge_kind == "reads" for e in edges)


def test_module_sources_and_terragrunt_paths_resolve_to_files() -> None:
    wanted = wanted_paths("infra/modules/aws-vpc/main.tf", ["../aws-kms", "hashicorp/vpc/aws"])
    assert (
        "infra/modules/aws-kms/main.tf" in wanted and "infra/modules/aws-kms/outputs.tf" in wanted
    )
    assert not [w for w in wanted if "hashicorp" in w], "a registry source names nothing"
    present = {"infra/modules/aws-kms/main.tf", "infra/modules/aws-kms/variables.tf"}
    assert resolve_imports("infra/modules/aws-vpc/main.tf", ["../aws-kms"], present) == [
        "infra/modules/aws-kms/main.tf",
        "infra/modules/aws-kms/variables.tf",
    ]
    tg = parse_hcl("live/prod/vpc/terragrunt.hcl", TERRAGRUNT)
    by = {s.name: s for s in tg.symbols}
    assert by["include.root"].kind == "include" and by["dependency.kms"].kind == "dependency"
    assert by["dependency.kms"].signature == 'dependency "../kms"'
    assert tg.includes == ["find_in_parent_folders()", "../../modules//aws-vpc", "../kms"]
    wanted = wanted_paths("live/prod/vpc/terragrunt.hcl", tg.includes)
    assert "live/prod/terragrunt.hcl" in wanted and "live/terragrunt.hcl" in wanted
    assert "live/modules/aws-vpc/main.tf" in wanted, "`//` joins the module subdirectory"
    assert "live/prod/kms/terragrunt.hcl" in wanted
    present = {
        "live/terragrunt.hcl",
        "live/prod/kms/terragrunt.hcl",
        "live/modules/aws-vpc/main.tf",
    }
    resolved = resolve_imports("live/prod/vpc/terragrunt.hcl", tg.includes, present)
    assert resolved == [
        "live/terragrunt.hcl",
        "live/modules/aws-vpc/main.tf",
        "live/prod/kms/terragrunt.hcl",
    ], "nearest ancestor first, one file"
    assert [n for _l, n, _k in reference_sites(tg)] == ["dependency.kms"]


def test_detection_chunks_and_tfvars() -> None:
    assert detect_by_extension("infra/main.tf") == "hcl"
    assert detect_by_extension("live/prod/terragrunt.hcl") == "hcl"
    pf = parse_hcl("infra/modules/aws-vpc/main.tf", MAIN)
    chunks = span_chunks(pf, 6000, **CHUNK_OPTIONS)
    kinds = [c.kind for c in chunks]
    assert kinds.count("function") >= 6 and "file_header" in kinds
    tfvars = parse_hcl("live/prod/prod.tfvars", b'region = "us-east-2"\ncidr = "10.0.0.0/16"\n')
    assert tfvars.symbols == [] and reference_sites(tfvars) == []


PROVIDERS = b"""provider "aws" {
  region = "us-east-1"
}

provider "aws" {
  alias  = "west"
  region = "us-west-2"
}

resource "aws_s3_bucket" "logs" {
  provider = aws.west
  count    = var.enabled ? 1 : 0
  bucket   = "logs-${count.index}"
}

resource "aws_subnet" "per_az" {
  for_each   = toset(var.azs)
  vpc_id     = aws_vpc.main.id
  depends_on = [aws_s3_bucket.logs, module.kms]
}

module "kms" {
  source    = "../aws-kms"
  providers = { aws = aws.west }
}

resource "aws_vpc" "main" {
  cidr_block = var.cidr
}
"""


def test_provider_aliases_repetition_and_depends_on() -> None:
    """An aliased provider is `provider.<name>.<alias>`,
    `provider = aws.west` / `providers = { aws = aws.west }` read it, a
    repeated block says `[count]` / `[for_each]`, `depends_on` reads."""
    pf = parse_hcl("infra/net/main.tf", PROVIDERS)
    by = {s.name: s for s in pf.symbols}
    assert by["provider.aws"].signature == 'provider "aws"'
    assert by["provider.aws.west"].kind == "provider"
    assert by["provider.aws.west"].signature == 'provider "aws" alias "west"'
    assert by["aws_s3_bucket.logs"].signature == 'resource "aws_s3_bucket" [count]'
    assert by["aws_subnet.per_az"].signature == 'resource "aws_subnet" [for_each]'
    assert by["module.kms"].signature == 'module source="../aws-kms"'
    assert by["aws_vpc.main"].signature == 'resource "aws_vpc"', "no repetition, no marker"
    sites = reference_sites(pf)
    assert (11, "provider.aws.west", "provider_ref") in sites
    assert (24, "provider.aws.west", "provider_ref") in sites, "the module's providers map"
    assert ("aws_s3_bucket.logs", "resource_ref") in {(n, k) for _l, n, k in sites}
    assert ("module.kms", "module_ref") in {(n, k) for _l, n, k in sites}, "depends_on entries read"
    assert not [n for _l, n, _k in sites if n.startswith("aws.")], "`aws.west` is never a resource"
    table = {s.name: [(s.id, s.qualified_name, s.kind)] for s in pf.symbols}
    edges = bind_sites(pf, sites, table, SITE_RULES)
    got = {
        (e.from_qualified_name.split("::")[1], e.to_qualified_name.split("::")[1]) for e in edges
    }
    assert ("aws_s3_bucket.logs", "provider.aws.west") in got
    assert ("module.kms", "provider.aws.west") in got
    assert ("aws_subnet.per_az", "aws_s3_bucket.logs") in got and (
        "aws_subnet.per_az",
        "module.kms",
    ) in got


def test_an_escaped_interpolation_ends_its_string_at_its_quote() -> None:
    for esc in ("$${", "%%{"):
        src = f'locals {{\n  a = "{esc}"\n}}\nresource "aws_s3_bucket" "b" {{}}\n'.encode()
        pf = parse_hcl("infra/main.tf", src)
        assert [(s.name, s.start_line, s.end_line) for s in pf.symbols] == [
            ("local.a", 2, 3),
            ("aws_s3_bucket.b", 4, 4),
        ], esc
