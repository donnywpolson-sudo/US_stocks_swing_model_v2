[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "HostProfile",
        "SelfTest",
        "Preflight",
        "PlanBatches",
        "ReadReferenceBatch",
        "ReadTargetBatch",
        "FinalPreflight"
    )]
    [string]$Mode,
    [string]$EnvelopePath,
    [string]$EnvelopeSha256,
    [int]$CommandOrdinal = 0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Utf8NoBomStrict = New-Object System.Text.UTF8Encoding($false, $true)
$ScriptPath = $MyInvocation.MyCommand.Path

function ConvertTo-Hex {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    return ([BitConverter]::ToString($Bytes)).Replace("-", "").ToLowerInvariant()
}

function Get-Sha256Bytes {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ConvertTo-Hex -Bytes $algorithm.ComputeHash($Bytes)
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-Sha1Bytes {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $algorithm = [Security.Cryptography.SHA1]::Create()
    try {
        return ConvertTo-Hex -Bytes $algorithm.ComputeHash($Bytes)
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-FileBytes {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [switch]$AllowOrdinaryHardLink
    )
    $item = Get-Item -LiteralPath $LiteralPath -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "path is a reparse point: $LiteralPath"
    }
    $linkType = $item.PSObject.Properties["LinkType"]
    if (-not $item.PSIsContainer -and $null -ne $linkType -and $linkType.Value) {
        if (-not ($AllowOrdinaryHardLink -and $linkType.Value -eq "HardLink")) {
            throw "path is linked: $LiteralPath"
        }
    }
    if ($item.PSIsContainer) {
        throw "path is not an ordinary file: $LiteralPath"
    }
    return [IO.File]::ReadAllBytes($item.FullName)
}

function Get-FileSha256 {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [switch]$AllowOrdinaryHardLink
    )
    return Get-Sha256Bytes -Bytes (
        Get-FileBytes -LiteralPath $LiteralPath `
            -AllowOrdinaryHardLink:$AllowOrdinaryHardLink
    )
}

function Get-GitBlobSha1 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $header = [Text.Encoding]::ASCII.GetBytes("blob $($Bytes.Length)`0")
    $stream = New-Object IO.MemoryStream
    try {
        $stream.Write($header, 0, $header.Length)
        $stream.Write($Bytes, 0, $Bytes.Length)
        return Get-Sha1Bytes -Bytes $stream.ToArray()
    }
    finally {
        $stream.Dispose()
    }
}

function Get-ContainedRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Candidate
    )
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd("\")
    $candidateFull = [IO.Path]::GetFullPath($Candidate)
    $prefix = $rootFull + "\"
    if ($candidateFull.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        return ""
    }
    if (-not $candidateFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "path escapes the declared root"
    }
    $relative = $candidateFull.Substring($prefix.Length)
    if ($relative.Contains(":")) {
        throw "alternate data streams are prohibited"
    }
    $cursor = $rootFull
    foreach ($part in $relative.Split("\")) {
        if (-not $part) {
            throw "empty path component is prohibited"
        }
        $cursor = Join-Path $cursor $part
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "reparse path component is prohibited"
            }
        }
    }
    return $relative.Replace("\", "/")
}

function Get-HostProfile {
    $processPath = (Get-Process -Id $PID).Path
    $file = Get-Item -LiteralPath $processPath -Force
    return [ordered]@{
        powershell_executable = $processPath
        powershell_sha256 = Get-FileSha256 -LiteralPath $processPath -AllowOrdinaryHardLink
        powershell_file_version = $file.VersionInfo.FileVersion
        ps_version = $PSVersionTable.PSVersion.ToString()
        ps_edition = $PSVersionTable.PSEdition
        clr_version = [Environment]::Version.ToString()
        is_64bit_process = [Environment]::Is64BitProcess
        sha256_hash_data_available = (
            [Security.Cryptography.SHA256].GetMethods().Name -contains "HashData"
        )
        sha1_hash_data_available = (
            [Security.Cryptography.SHA1].GetMethods().Name -contains "HashData"
        )
        path_get_relative_path_available = (
            [IO.Path].GetMethods().Name -contains "GetRelativePath"
        )
    }
}

function Write-Json {
    param([Parameter(Mandatory = $true)][object]$Value)
    [Console]::Out.Write(($Value | ConvertTo-Json -Compress -Depth 20) + "`n")
}

function Read-Envelope {
    if (-not $EnvelopePath -or -not $EnvelopeSha256 -or $CommandOrdinal -le 0) {
        throw "envelope path, SHA-256, and command ordinal are required"
    }
    $raw = Get-FileBytes -LiteralPath $EnvelopePath
    if ((Get-Sha256Bytes -Bytes $raw) -ne $EnvelopeSha256) {
        throw "envelope file SHA-256 mismatch"
    }
    try {
        $value = $Utf8NoBomStrict.GetString($raw) | ConvertFrom-Json
    }
    catch {
        throw "envelope is not valid UTF-8 JSON"
    }
    if ($value.schema_version -ne 1) {
        throw "envelope schema is unsupported"
    }
    $commands = @($value.commands)
    if ($CommandOrdinal -gt $commands.Count) {
        throw "command ordinal is outside the envelope"
    }
    $command = $commands[$CommandOrdinal - 1]
    if ($command.command_ordinal -ne $CommandOrdinal -or $command.mode -ne $Mode) {
        throw "current mode differs from the exact command record"
    }
    $root = [IO.Path]::GetFullPath([string]$value.repository.root).TrimEnd("\")
    $current = [IO.Path]::GetFullPath((Get-Location).Path).TrimEnd("\")
    if (-not $current.Equals($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "working directory differs from the envelope"
    }
    $scriptPath = $script:ScriptPath
    $scriptRelative = Get-ContainedRelativePath -Root $root -Candidate $scriptPath
    if ($scriptRelative.Replace("\", "/") -ne [string]$value.script.path) {
        throw "executed script path differs from the envelope"
    }
    $scriptBytes = Get-FileBytes -LiteralPath $scriptPath
    if (
        (Get-Sha256Bytes -Bytes $scriptBytes) -ne [string]$value.script.sha256 -or
        (Get-GitBlobSha1 -Bytes $scriptBytes) -ne [string]$value.script.git_blob
    ) {
        throw "executed script identity differs from the envelope"
    }
    $profile = Get-HostProfile
    foreach ($field in @(
        "powershell_executable",
        "powershell_sha256",
        "powershell_file_version",
        "ps_version",
        "ps_edition",
        "clr_version",
        "is_64bit_process",
        "sha256_hash_data_available",
        "sha1_hash_data_available",
        "path_get_relative_path_available"
    )) {
        $expectedProperty = $value.host.PSObject.Properties[$field]
        if ($null -eq $expectedProperty -or [string]$profile[$field] -ne [string]$expectedProperty.Value) {
            throw "host capability differs from the envelope: $field"
        }
    }
    return [pscustomobject]@{
        Value = $value
        Command = $command
        Root = $root
    }
}

function Invoke-RepositoryPreflight {
    param([Parameter(Mandatory = $true)][object]$Context)
    $value = $Context.Value
    $rootItem = Get-Item -LiteralPath $Context.Root -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "repository root is a reparse point"
    }
    function Invoke-Git {
        param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
        $output = & git.exe -C $Context.Root @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Git preflight command failed"
        }
        return ($output -join "`n").Trim()
    }
    if (
        -not (Invoke-Git rev-parse --show-toplevel).Replace("/", "\").Equals(
            $Context.Root, [StringComparison]::OrdinalIgnoreCase
        ) -or
        (Invoke-Git branch --show-current) -ne [string]$value.repository.branch -or
        (Invoke-Git rev-parse HEAD) -ne [string]$value.repository.head -or
        (Invoke-Git rev-parse "HEAD^{tree}") -ne [string]$value.repository.tree
    ) {
        throw "Git identity differs from the envelope"
    }
    if ($value.repository.require_clean -and (Invoke-Git status --porcelain=v1 --untracked-files=all)) {
        throw "repository is not clean"
    }
    foreach ($bindingName in @("target", "controller")) {
        $binding = $value.$bindingName
        $path = Join-Path $Context.Root ([string]$binding.path).Replace("/", "\")
        if ((Get-ContainedRelativePath -Root $Context.Root -Candidate $path) -ne [string]$binding.path) {
            throw "$bindingName path differs"
        }
        $bytes = Get-FileBytes -LiteralPath $path
        if (
            $bytes.Length -ne [int64]$binding.bytes -or
            (Get-Sha256Bytes -Bytes $bytes) -ne [string]$binding.sha256 -or
            (Get-GitBlobSha1 -Bytes $bytes) -ne [string]$binding.git_blob
        ) {
            throw "$bindingName identity differs"
        }
    }
    return [ordered]@{
        mode = $Mode
        command_ordinal = $CommandOrdinal
        repository_head = [string]$value.repository.head
        repository_tree = [string]$value.repository.tree
        target_sha256 = [string]$value.target.sha256
    }
}

function Invoke-ReadBatch {
    param(
        [Parameter(Mandatory = $true)][object]$Context,
        [Parameter(Mandatory = $true)][bool]$Target
    )
    $command = $Context.Command
    if ($null -eq $command.batch_ordinal) {
        throw "read command lacks a batch ordinal"
    }
    $batches = @($Context.Value.read_batches)
    $batch = $batches[[int]$command.batch_ordinal - 1]
    if ([bool]$batch.target -ne $Target) {
        throw "batch target applicability differs from the mode"
    }
    $path = Join-Path $Context.Root ([string]$batch.path).Replace("/", "\")
    if ((Get-ContainedRelativePath -Root $Context.Root -Candidate $path) -ne [string]$batch.path) {
        throw "batch path differs"
    }
    $bytes = Get-FileBytes -LiteralPath $path
    if (
        (Get-Sha256Bytes -Bytes $bytes) -ne [string]$batch.file_sha256 -or
        (Get-GitBlobSha1 -Bytes $bytes) -ne [string]$batch.file_git_blob
    ) {
        throw "batch file identity differs"
    }
    $text = $Utf8NoBomStrict.GetString($bytes)
    if ($text.Contains("`r")) {
        throw "batch text must use LF line endings"
    }
    $lines = @($text.Split("`n"))
    if ($text.EndsWith("`n")) {
        $lines = @($lines[0..($lines.Count - 2)])
    }
    $start = [int]$batch.start_line
    $count = [int]$batch.line_count
    if ($start -gt $lines.Count + 1) {
        throw "batch begins beyond the file"
    }
    $available = [Math]::Max(0, [Math]::Min($count, $lines.Count - $start + 1))
    if ($available -le 0) {
        throw "batch contains no readable lines"
    }
    $end = $start + $available - 1
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append("===== $($batch.path) lines $start-$end =====`n")
    for ($offset = 0; $offset -lt $available; $offset++) {
        [void]$builder.Append(("{0:D6}: {1}`n" -f ($start + $offset), $lines[$start - 1 + $offset]))
    }
    $rendered = $builder.ToString()
    $renderedBytes = $Utf8NoBomStrict.GetByteCount($rendered)
    if (
        $renderedBytes -gt [int]$batch.max_rendered_utf8_bytes -or
        $renderedBytes -gt [int]$command.output_max_utf8_bytes
    ) {
        throw "batch output exceeds its exact byte limit"
    }
    [Console]::Out.Write($rendered)
}

if ($Mode -eq "HostProfile") {
    Write-Json -Value (Get-HostProfile)
    exit 0
}

if ($Mode -eq "SelfTest") {
    $known = $Utf8NoBomStrict.GetBytes("abc")
    if (
        (Get-Sha256Bytes -Bytes $known) -ne
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" -or
        (Get-Sha1Bytes -Bytes $known) -ne
            "a9993e364706816aba3e25717850c26c9cd0d89d"
    ) {
        throw "instance hashing self-test failed"
    }
    $selfRelative = Get-ContainedRelativePath -Root (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) -Candidate $script:ScriptPath
    Write-Json -Value ([ordered]@{
        mode = "SELF_TEST_NO_WRITES"
        hashing = "PASS"
        contained_relative_path = $selfRelative
    })
    exit 0
}

$context = Read-Envelope
switch ($Mode) {
    "Preflight" {
        Write-Json -Value (Invoke-RepositoryPreflight -Context $context)
    }
    "PlanBatches" {
        Write-Json -Value ([ordered]@{
            mode = "PLAN_BATCHES_NO_WRITES"
            command_ordinal = $CommandOrdinal
            reference_census_count = [int]$context.Value.reference_census.count
            read_batch_count = @($context.Value.read_batches).Count
        })
    }
    "ReadReferenceBatch" {
        Invoke-ReadBatch -Context $context -Target $false
    }
    "ReadTargetBatch" {
        Invoke-ReadBatch -Context $context -Target $true
    }
    "FinalPreflight" {
        Write-Json -Value (Invoke-RepositoryPreflight -Context $context)
    }
}
