<#
.SYNOPSIS
    Support N2 : état de santé d'une file d'attente Orchestrator.

.DESCRIPTION
    Affiche la répartition des éléments par statut et les motifs d'exceptions métier, puis renvoie un
    code de sortie exploitable par un outil de supervision :
      0 = OK, 1 = éléments en échec système à traiter, 2 = éléments abandonnés (robot planté).
    Compatible Windows PowerShell 5.1 et PowerShell 7.

.EXAMPLE
    .\ops\Get-QueueHealth.ps1 -QueueName FACTURES_FOURNISSEURS
#>
[CmdletBinding()]
param(
    [string]$OrchestratorUrl = "http://127.0.0.1:8090",
    [string]$QueueName = "FACTURES_FOURNISSEURS"
)
$ErrorActionPreference = "Stop"
# Invoke-RestMethod emits a JSON array as a single object in Windows PowerShell 5.1, so an empty "[]"
# would count as one item; piping through ForEach-Object unrolls it.

$stats = Invoke-RestMethod -Uri "$OrchestratorUrl/api/queues/$QueueName/stats"
$failedSystem = @(Invoke-RestMethod -Uri "$OrchestratorUrl/api/queues/$QueueName/items?status=Failed&exception_type=ApplicationException" | ForEach-Object { $_ })
$abandoned = @(Invoke-RestMethod -Uri "$OrchestratorUrl/api/queues/$QueueName/items?status=Abandoned" | ForEach-Object { $_ })

Write-Host "File : $QueueName"
$stats.by_status.PSObject.Properties | Sort-Object Name |
    Select-Object @{n = "Statut"; e = { $_.Name } }, @{n = "Elements"; e = { $_.Value } } | Format-Table -AutoSize
if ($stats.business_exceptions_by_reason.PSObject.Properties.Count -gt 0) {
    Write-Host "Exceptions metier (a traiter par le metier, pas par le support) :"
    $stats.business_exceptions_by_reason.PSObject.Properties |
        Select-Object @{n = "Motif"; e = { $_.Name } }, @{n = "Elements"; e = { $_.Value } } | Format-Table -AutoSize
}
Write-Host ("Duree moyenne de traitement : {0} s" -f $stats.avg_processing_seconds)

if ($abandoned.Count -gt 0) {
    Write-Warning "$($abandoned.Count) element(s) Abandoned : un robot s'est arrete en cours de transaction."
    exit 2
}
if ($failedSystem.Count -gt 0) {
    Write-Warning "$($failedSystem.Count) element(s) en echec systeme apres toutes les relances automatiques."
    $failedSystem | Select-Object Id, @{n = "Fichier"; e = { $_.SpecificContent.FileName } }, RetryNumber,
        @{n = "Detail"; e = { $_.ProcessingException.Details } } | Format-Table -AutoSize -Wrap
    exit 1
}
Write-Host "OK : aucune action support requise."
exit 0
