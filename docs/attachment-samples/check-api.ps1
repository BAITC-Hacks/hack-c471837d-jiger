param([string]$BaseUrl = 'http://127.0.0.1:8000')

$ErrorActionPreference = 'Stop'
$sampleDirectory = $PSScriptRoot

function Test-Upload {
    param([string]$Name, [int]$ExpectedStatus, [string[]]$Fields)

    $curlArgs = @('--silent', '--show-error', '--max-time', '30', '--request', 'POST', '--write-out', "`n%{http_code}")
    foreach ($field in $Fields) {
        $curlArgs += @('--form', $field)
    }
    $curlArgs += $BaseUrl.TrimEnd('/') + '/chat'
    $responseLines = & curl.exe @curlArgs
    if ($LASTEXITCODE -ne 0) { throw "$Name : curl failed. Check that the server is running at $BaseUrl." }
    $responseText = $responseLines -join "`n"
    $separator = $responseText.LastIndexOf("`n")
    if ($separator -lt 0) { throw "$Name : invalid HTTP response." }
    $status = [int]$responseText.Substring($separator + 1).Trim()
    $body = $responseText.Substring(0, $separator) | ConvertFrom-Json
    if ($status -ne $ExpectedStatus) { throw "$Name : expected HTTP $ExpectedStatus, got $status." }
    if ([string]::IsNullOrWhiteSpace($body.reply)) { throw "$Name : missing reply in JSON." }
    Write-Output "PASS $Name : HTTP $status, reply present"
}

Test-Upload 'unsupported-extension' 415 @("files=@$sampleDirectory/11-unsupported.txt;type=text/plain")
Test-Upload 'wrong-content-type' 415 @("files=@$sampleDirectory/03-invoice.pdf;type=image/jpeg")
Test-Upload 'corrupted-pdf' 422 @("files=@$sampleDirectory/12-corrupted.pdf;type=application/pdf")
Test-Upload 'empty-file' 422 @("files=@$sampleDirectory/13-empty.pdf;type=application/pdf")
Test-Upload 'file-over-10mb' 413 @("files=@$sampleDirectory/14-too-large.pdf;type=application/pdf")
Test-Upload 'four-files' 413 @(
    "files=@$sampleDirectory/05-product.jpg;type=image/jpeg",
    "files=@$sampleDirectory/05-product.jpg;type=image/jpeg",
    "files=@$sampleDirectory/05-product.jpg;type=image/jpeg",
    "files=@$sampleDirectory/05-product.jpg;type=image/jpeg"
)
Write-Output 'All 6 upload validation checks passed.'
