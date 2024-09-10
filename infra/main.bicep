targetScope = 'subscription'

// The main bicep module to provision Azure resources.
// For a more complete walkthrough to understand how this file works with azd,
// see https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/make-azd-compatible?pivots=azd-create

@minLength(1)
@maxLength(64)
@description('Name of the the environment which is used to generate a short unique hash used in all resources.')
param environmentName string

@minLength(1)
@description('Primary location for core resources')
param location string

param resourceGroupName string = ''

param searchServiceName string = ''
param searchServiceSkuName string // Set in main.parameters.json

@description('Location for the AI Search Service to support semantic search')
@allowed(['brazilsouth', 'canadacentral', 'eastUS', 'eastUS2', 'northcentralus', 'southcentralus', 'westus', 'westcentralus', 'uksouth', 'switzerlandwest', 'switzerlandeast', 'francecentral', 'australiaeast', 'eastasia', 'southeastasia​', 'centralindia', 'japanwest', 'koreacentral​'])
@metadata({
  azd: {
    type: 'location'
  }
})
param searchServiceLocation string
param searchIndexName string // Set in main.parameters.json

param storageAccountName string = ''
param storageContainerName string // Set in main.parameters.json

param keyvaultName string = ''

param databaseName string = 'maintenance-requests'
param sqlServerName string = ''

@secure()
param sqlAdminPassword string = ''
@secure()
param appUserPassword string = ''

param openAiServiceName string = ''
param openAiSkuName string // Set in main.parameters.json

@description('Location for the OpenAI')
@allowed(['eastus', 'eastus2', 'francecentral', 'westus', 'northcentralus', 'southcentralus', 'westeurope', 'swedencentral'])
@metadata({
  azd: {
    type: 'location'
  }
})
param openAiLocation string

param embeddingDeploymentName string = 'text-embedding-ada-002'
param embeddingDeploymentCapacity int = 30
param embeddingModelName string = 'text-embedding-ada-002'
param embeddingModelVersion string = '2'

param gptVisionDeploymentName string = 'gpt-4-turbo'
param gptVisionDeploymentCapacity int = 50
param gptVisionModelName string = 'gpt-4'
param gptVisionModelVersion string = 'turbo-2024-04-09'


//@description('Id of the user or app to assign application roles')
//param principalId string = ''

//@description('Flag to decide where to create roles for current user')
//param createRoleForUser bool = true

// Optional parameters to override the default azd resource naming conventions.
// Add the following to main.parameters.json to provide values:
// "resourceGroupName": {
//      "value": "myGroupName"
// }

var abbrs = loadJsonContent('./abbreviations.json')

// tags that should be applied to all resources.
var tags = {
  // Tag all resources with the environment name.
  'azd-env-name': environmentName
}

// Generate a unique token to be used in naming resources.
// Remove linter suppression after using.
#disable-next-line no-unused-vars
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

// Name of the service defined in azure.yaml
// A tag named azd-service-name with this value should be applied to the service host resource, such as:
//   Microsoft.Web/sites for appservice, function
// Example usage:
//   tags: union(tags, { 'azd-service-name': apiServiceName })
#disable-next-line no-unused-vars
var apiServiceName = 'python-api'

// Organize resources in a resource group
resource resourceGroup 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: !empty(resourceGroupName) ? resourceGroupName : '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

// Add resources to be provisioned below.
// A full example that leverages azd bicep modules can be seen in the todo-python-mongo template:
// https://github.com/Azure-Samples/todo-python-mongo/tree/main/infra

// Storage Account
module storage 'core/storage/storage-account.bicep' = {
  name: 'storage'
  scope: resourceGroup
  params: {
    name: !empty(storageAccountName) ? storageAccountName : '${abbrs.storageStorageAccounts}${resourceToken}'
    location: location
    tags: tags
    publicNetworkAccess: 'Enabled'
    allowBlobPublicAccess: false
    sku: {
      name: 'Standard_LRS'
    }
    deleteRetentionPolicy: {
      enabled: true
      days: 2
    }
    containers: [
      {
        name: storageContainerName
        publicAccess: 'None'
      }
    ]
  }
}

// Keyvault

module keyvault 'core/security/keyvault.bicep' = {
  name: 'keyvault'
  scope: resourceGroup
  params: {
    name: !empty(keyvaultName) ? keyvaultName : '${abbrs.keyVaultVaults}${resourceToken}'
    location: location
    tags: tags
  }
}

// Azure SQL

module sql 'core/database/sqlserver/sqlserver.bicep' = {
  name: 'sql'
  scope: resourceGroup
  params: {
    name: !empty(sqlServerName) ? sqlServerName : '${abbrs.sqlServers}${resourceToken}'
    location: location
    tags: tags
    databaseName: !empty(databaseName) ? databaseName : '${abbrs.sqlServersDatabases}${resourceToken}'
    keyVaultName: keyvault.outputs.name
    connectionStringKey: 'AZURE-SQL-CONNECTION-STRING'
    sqlAdminPassword: sqlAdminPassword
    appUserPassword: appUserPassword
  }
}

// Open AI

module openAi 'core/ai/aiservices.bicep' = {
  name: 'openai'
  scope: resourceGroup
  params: {
    name: !empty(openAiServiceName) ? openAiServiceName : '${abbrs.cognitiveServicesAccounts}${resourceToken}'
    location: openAiLocation
    tags: tags
    sku: {
      name: openAiSkuName
    }
    deployments: [
      {
        name: embeddingDeploymentName
        model: {
          format: 'OpenAI'
          name: embeddingModelName
          version: embeddingModelVersion
        }
        capacity: embeddingDeploymentCapacity
      }
      {
        name: gptVisionDeploymentName
        model: {
          format: 'OpenAI'
          name: gptVisionModelName
          version: gptVisionModelVersion
        }
        capacity: gptVisionDeploymentCapacity
      }
    ]
  }
}

// Azure Search

module searchService 'core/search/search-services.bicep' = {
  name: 'search-service'
  scope: resourceGroup
  params: {
    name: !empty(searchServiceName) ? searchServiceName : 'acsvector-${resourceToken}'
    location: searchServiceLocation
    tags: tags
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
    sku: {
      name: searchServiceSkuName
    }
    semanticSearch: 'free'
  }
}


// Add outputs from the deployment here, if needed.
//
// This allows the outputs to be referenced by other bicep deployments in the deployment pipeline,
// or by the local machine as a way to reference created resources in Azure for local development.
// Secrets should not be added here.
//
// Outputs are automatically saved in the local azd environment .env file.
// To see these outputs, run `azd env get-values`,  or `azd env get-values --output json` for json output.
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId

output AZURE_RESOURCE_GROUP string = resourceGroup.name

output AZURE_OAI_EMBED_DEPLOYMENT_NAME string = embeddingDeploymentName
output AZURE_OAI_ENDPOINT string = openAi.outputs.endpoint
output AZURE_OAI_API_KEY string = openAi.outputs.key
output AZURE_OAI_GPTVISION_DEPLOYMENT_NAME string = gptVisionDeploymentName
output AZURE_OPENAI_SERVICE_NAME string = openAi.outputs.name

output AZURE_SEARCH_SERVICE_ENDPOINT string = searchService.outputs.endpoint
output AZURE_SEARCH_API_KEY string = searchService.outputs.key
output AZURE_SEARCH_SERVICE_NAME string = searchService.outputs.name
output AZURE_SEARCH_INDEX_NAME string = searchIndexName

output AZURE_STORAGE_ACCOUNT_NAME string = storage.outputs.name
output AZURE_STORAGE_CONTAINER string = storageContainerName
output AZURE_BLOB_CONNECTION_STRING string = storage.outputs.blobConnectionString

output AZURE_KEYVAULT_NAME string = keyvault.outputs.name

output AZURE_SQL_SERVER_NAME string = sql.outputs.serverName
output AZURE_SQL_DATABASE_NAME string = sql.outputs.databaseName

output AZURE_PYTHON_SQL_CONNECTION_STRING string = sql.outputs.pythonConnectionString
