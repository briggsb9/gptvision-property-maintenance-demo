import os
import base64
import json
import logging
import asyncio
import aiohttp
import aiofiles
import aioodbc
import random
import datetime
from openai import AsyncAzureOpenAI
from azure.core.credentials import AzureKeyCredential
from azure.storage.blob.aio import BlobServiceClient
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SimpleField,
    SearchFieldDataType,
    SearchableField,
    SearchField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticPrioritizedFields,
    SemanticField,
    SemanticSearch,
    SearchIndex,
    AzureOpenAIVectorizer,
    AzureOpenAIParameters
)
from azure.identity import DefaultAzureCredential

# Configuration
OAI_API_ENDPOINT = os.getenv("AZURE_OAI_ENDPOINT")
OAI_API_KEY = os.getenv("AZURE_OAI_API_KEY")
OAI_EMBED_DEPLOYMENT_NAME = os.getenv("AZURE_OAI_EMBED_DEPLOYMENT_NAME") or "text-embedding-ada-002"
OAI_GPTVISION_DEPLOYMENT_NAME = os.getenv("AZURE_OAI_GPTVISION_DEPLOYMENT_NAME") or "gpt-4-turbo"
OAI_GPT4V_API_ENDPOINT = f"{os.getenv("AZURE_OAI_ENDPOINT")}openai/deployments/gpt-4-turbo/chat/completions?api-version=2024-02-15-preview"
SEARCH_SERVICE_ENDPOINT = os.getenv("AZURE_SEARCH_SERVICE_ENDPOINT")
SEARCH_INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX_NAME") or "maintenance-requests"
SEARCH_API_KEY = os.getenv("AZURE_SEARCH_API_KEY")
BLOB_CONNECTION_STRING = os.getenv("AZURE_BLOB_CONNECTION_STRING")
STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER") or "images"
SQL_CONNECTION_STRING = os.getenv("AZURE_PYTHON_SQL_CONNECTION_STRING")

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)  # Set the logging level to INFO

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)  # Set the console handler level to INFO

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)

logger.addHandler(console_handler)

# Suppress detailed Azure SDK logs
azure_logger = logging.getLogger('azure')
azure_logger.setLevel(logging.WARNING)  # Set to WARNING to suppress INFO logs

# Use DefaultAzureCredential for authentication
credential = DefaultAzureCredential()

# BloB Service Client
blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
container_client = blob_service_client.get_container_client(STORAGE_CONTAINER)

# Create a connection pool
async def create_pool():
    try:
        pool = await aioodbc.create_pool(dsn=SQL_CONNECTION_STRING, minsize=1, maxsize=10)
        logger.info("Connection pool created successfully.")
        return pool
    except Exception as e:
        logger.error(f"An error occurred while creating the connection pool: {e}")
        return None

def generate_random_date_within_last_6_months():
    end_date = datetime.datetime.now(datetime.timezone.utc)
    start_date = end_date - datetime.timedelta(days=180)
    random_date = start_date + (end_date - start_date) * random.random()
    return random_date.isoformat(timespec='milliseconds').replace('+00:00', 'Z')  # Ensure UTC format

def generate_random_job_assigned():
    return random.choice(["yes", "no"])


# Create the Azure SQL table
async def create_sql_table(pool):
    logger.info("Starting to create the Azure SQL table 'MaintenanceRequests'.")
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            try:
                # Check if the table exists
                await cursor.execute("""
                IF OBJECT_ID('dbo.MaintenanceRequests', 'U') IS NOT NULL
                SELECT 1
                ELSE
                SELECT 0
                """)
                table_exists = await cursor.fetchone()
                
                if table_exists[0] == 1:
                    # Drop the table if it exists
                    await cursor.execute("DROP TABLE dbo.MaintenanceRequests")
                    logger.info("Dropped existing table 'MaintenanceRequests'.")
                
                # Create the table
                await cursor.execute("""
                CREATE TABLE MaintenanceRequests (
                    CustomerID NVARCHAR(50),
                    CaseID NVARCHAR(50) PRIMARY KEY,
                    Description NVARCHAR(MAX),
                    ImageURL NVARCHAR(2083),
                    MouldDetected BIT,
                    FileName NVARCHAR(2083),
                    DateOpened Datetime2,
                    JobAssigned NVARCHAR(3)
                )
                """)
                await conn.commit()
                logger.info("Azure SQL table 'MaintenanceRequests' created successfully.")
            except aioodbc.Error as e:
                logger.error(f"Error occurred while creating the SQL table: {e}")


# Create the container if it does not exist
async def create_container_if_not_exists(container_client):
    try:
        if not await container_client.exists():
            await container_client.create_container()
            logger.info(f"Container '{STORAGE_CONTAINER}' created.")
        else:
            logger.info(f"Container '{STORAGE_CONTAINER}' already exists.")
    except Exception as e:
        logger.error(f"An error occurred while checking/creating the container: {e}")


# Upload an image to Azure Blob Storage
async def upload_image_to_blob(image_path, filename):
    try:
        async with container_client:
            # Create a BlobClient for the specific file
            blob_client = container_client.get_blob_client(filename)

            try:
                # Check if the blob exists
                await blob_client.get_blob_properties()
                logger.info(f"Image {filename} already exists in blob storage.")
                return blob_client.url
            except Exception as e:
                if "BlobNotFound" in str(e):
                    logger.info(f"Blob {filename} not found, proceeding with upload.")
                else:
                    logger.error(f"An error occurred while checking if {filename} exists: {e}")
                    return None

            # Blob does not exist, proceed with upload
            async with aiofiles.open(image_path, "rb") as file:
                file_data = await file.read()
            
            try:
                await blob_client.upload_blob(file_data, overwrite=True)
                logger.info(f"Image {filename} uploaded successfully.")
            except Exception as upload_error:
                logger.error(f"An error occurred while uploading {filename} to blob storage: {upload_error}")
                return None
            
            # Return the URL of the uploaded blob
            return blob_client.url

    except Exception as e:
        logger.error(f"An error occurred while processing {filename}: {e}")
        return None


# Insert a record into the MaintenanceRequests table
async def insert_into_sql_table(pool, customer_id, case_id, description, image_url, mould_detected, file_name, date_opened, job_assigned):
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            try:
                await cursor.execute("""
                INSERT INTO MaintenanceRequests (CustomerID, CaseID, Description, ImageURL, MouldDetected, FileName, DateOpened, JobAssigned)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (customer_id, case_id, description, image_url, mould_detected, file_name, date_opened, job_assigned))
                await conn.commit()
            except Exception as e:
                logger.error(f"An error occurred: {e}")


# Update a record in the MaintenanceRequests table
async def update_maintenance_request(pool, case_id, description, mould_detected):
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            try:
                await cursor.execute("""
                UPDATE MaintenanceRequests
                SET Description = ?, MouldDetected = ?
                WHERE CaseID = ?
                """, (description, mould_detected, case_id))
                await conn.commit()
            except Exception as e:
                logger.error(f"An error occurred while updating case {case_id}: {e}")


# Create dummy database with images from the data folder
async def create_dummy_database(pool, data_folder):
    try:
        tasks = []
        for entry in os.scandir(data_folder):
            if entry.is_file() and entry.name.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp')):
                image_path = os.path.join(data_folder, entry.name)
                tasks.append(asyncio.create_task(process_image(pool, image_path, entry.name)))
        
        await asyncio.gather(*tasks)
    except Exception as e:
        logger.error(f"An error occurred while accessing the data folder {data_folder}: {e}")

async def init_openai_client():
    return AsyncAzureOpenAI(
        api_key=OAI_API_KEY,
        api_version="2024-02-01",
        azure_endpoint=OAI_API_ENDPOINT
    )


# Generate vector representation using Azure OpenAI embedding model
async def generate_vector(description):
    client = await init_openai_client()
    try:
        response = await client.embeddings.create(
            input=description,
            model=OAI_EMBED_DEPLOYMENT_NAME
        )
        vector = response.data[0].embedding
        return vector
    except Exception as e:
        logger.error(f"An error occurred while generating the vector for the description: {e}")
        return None


# Generate a description using GPT-4 Vision
async def generate_image_description(image_data):
    headers = {
        "Content-Type": "application/json",
        "api-key": OAI_API_KEY,
    }
    payload = {
        "messages": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "As an AI assistant for a housing association, your primary task is to provide a short description of the image, the repair thats needed, and the tradesman needed to complete the job. In addition, a key focus is the identification of mould which should be rated according to severity in all responses. Therefore, if mould is present in the image always add the words MOULD DETECTED. If no mould is present in the image always add the words MOULD NOT DETECTED. Your response should always follow the heading order and content of, Image Description, Repair needed, Tradesman Required, and finally Mould Status. Always in that order, no exceptions. Do not format with any special characters. Remember to provide accurate and concise answers based on the information present in the image and use external knowledge of building maintenance. Your response should not provide a request for more info as this info will be injected into an AI Search index field."
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Do as your system message instructs for the provided image."
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data}"
                        }
                    },
                ]
            }
        ],
        "temperature": 0.7,
        "top_p": 0.95,
        "max_tokens": 800
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(OAI_GPT4V_API_ENDPOINT, headers=headers, json=payload) as response:
                response.raise_for_status()
                response_json = await response.json()
                
                # Check if the response contains the expected data
                if 'choices' in response_json and len(response_json['choices']) > 0:
                    description = response_json['choices'][0]['message']['content']
                    return description
                else:
                    raise ValueError("The response does not contain the expected 'choices' data.")
                    
        except aiohttp.ClientError as e:
            logger.error(f"An HTTP error occurred: {e}")
        except ValueError as e:
            logger.error(f"An error occurred while processing the response: {e}")
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")
    
    return None  # Return None if an error occurred or the description was not generated


# Function to detect mould status from description
def detect_mould_status(description):
    try:
        description_upper = description.upper()  # Convert description to uppercase for case-insensitive matching
        if "MOULD DETECTED" in description_upper:
            return True
        elif "MOULD NOT DETECTED" in description_upper:
            return False
        else:
            # Default to False if the specific phrases are not found
            return False
    except Exception as e:
        logger.error(f"An error occurred while detecting mould status: {e}")
        return False


# Process image for uploading to Azure Blob Storage and inserting into SQL table
async def process_image(pool, image_path, filename):
    try:
        # Upload image to Azure Blob Storage and get the URL
        image_url = await upload_image_to_blob(image_path, filename)

        # Generate dummy data for CustomerID and CaseID
        customer_id = str(random.randint(1000, 9999))
        case_id = str(random.randint(100000, 999999))
        date_opened = generate_random_date_within_last_6_months()
        job_assigned = generate_random_job_assigned()

        # Insert data into SQL table using the helper function
        await insert_into_sql_table(pool, customer_id, case_id, "", image_url, False, filename, date_opened, job_assigned)

        logger.info(f"Processed {filename}")
    except Exception as e:
        logger.error(f"An error occurred while processing {filename}: {e}")


# Read blob data from Azure Blob Storage to avoid service to service authentication
async def read_blob_data(container_client, blob_name):
    try:
        blob_client = container_client.get_blob_client(blob=blob_name)
        blob_data = await blob_client.download_blob()
        image_data = await blob_data.readall()

        return image_data

    except Exception as e:
        logger.error(f"An error occurred while reading the blob {blob_name}: {e}")
        return None
    finally:
        # Manually close the blob client if async with is not available
        await blob_client.close()


# Processes cases for indexing adding GenAI generated descriptions. Outputs a JSON file with the index data.    
async def process_cases_for_indexing(pool, json_file_path):
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            try:
                await cursor.execute("SELECT * FROM MaintenanceRequests")
                rows = await cursor.fetchall()
                data = []
                tasks = []
                for row in rows:
                    customer_id, case_id, description, image_url, mould_detected, file_name, date_opened, job_assigned = row
                    blob_name = image_url.split('/')[-1]
                    image_data = await read_blob_data(container_client, blob_name)
                    blob_base64 = base64.b64encode(image_data).decode('utf-8')
                    tasks.append(asyncio.create_task(process_case(pool, blob_base64, case_id, customer_id, file_name, image_url, date_opened, job_assigned, data)))
                
                await asyncio.gather(*tasks)
                
                # Write data to JSON file
                async with aiofiles.open(json_file_path, 'w') as json_file:
                    await json_file.write(json.dumps(data, indent=4))
                logger.info(f"JSON file {json_file_path} created successfully.")
                return data
            except Exception as e:
                logger.error(f"An error occurred: {e}")
                return None


async def process_case(pool, blob_base64, case_id, customer_id, file_name, image_url, date_opened, job_assigned, data):
    try:
        # Generate the image description
        description = await generate_image_description(blob_base64)

        # Detect mould status from the description
        mould_detected = detect_mould_status(description)
        logger.info(f"Mould detected for case {case_id}: {mould_detected}")

        # Update the database with the new description and mould status
        await update_maintenance_request(pool, case_id, description, mould_detected)

        # Generate the vector representation of the description
        vector = await generate_vector(description)

        # Append the processed data to the list
        data.append({
            "FileName": file_name,
            "CustomerID": customer_id,
            "CaseID": case_id,
            "Description": description,
            "ImageURL": image_url,
            "MouldDetected": mould_detected,
            "Vector": vector,
            "DateOpened": date_opened.isoformat(),
            "JobAssigned": job_assigned
        })
        logger.info(f"Processed case {case_id} for indexing")
    except Exception as e:
        logger.error(f"An error occurred while processing case {case_id}: {e}")


# Function to create the Azure AI search index
def create_search_index():
    try:
        index_client = SearchIndexClient(
            endpoint=SEARCH_SERVICE_ENDPOINT,
            credential=AzureKeyCredential(SEARCH_API_KEY),
        )

        # Check if the index already exists
        if SEARCH_INDEX_NAME in index_client.list_index_names():
            try:
                # Delete the existing index
                logger.info(f"Recreating search index {SEARCH_INDEX_NAME}")
                index_client.delete_index(SEARCH_INDEX_NAME)
                logger.info(f"Search Index {SEARCH_INDEX_NAME} deleted")
            except Exception as e:
                logger.error(f"An error occurred while deleting the search index {SEARCH_INDEX_NAME}: {e}")
                return

        # Define the fields and create the index
        fields = [
            SimpleField(
                name="FileName",
                type=SearchFieldDataType.String,
                filterable=True,
                sortable=True,
                facetable=True,
            ),
            SimpleField(
                name="MouldDetected",
                type=SearchFieldDataType.Boolean,
                filterable=True,
                sortable=True,
                facetable=True,
            ),
            SearchableField(
                name="DateOpened",
                type=SearchFieldDataType.String,  # Date stored as string for GenAI use case
                filterable=True,
                sortable=True,
                facetable=True,
            ),
            SearchableField(
                name="JobAssigned",
                type=SearchFieldDataType.String,
                filterable=True,
                sortable=True,
                facetable=True,
            ),
            SearchableField(
                name="CustomerID",
                type=SearchFieldDataType.String,
                filterable=True,
                sortable=True,
                facetable=True,
            ),
            SearchableField(
                name="CaseID",
                type=SearchFieldDataType.String,
                key=True,
                filterable=True,
                sortable=True,
                facetable=True,
            ),
            SearchableField(name="Description", type=SearchFieldDataType.String),
            SimpleField(name="ImageURL", type=SearchFieldDataType.String),
            SearchField(
                name="Vector",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=1536,
                vector_search_profile_name="myHnswProfile",
            ),
        ]

        vector_search = VectorSearch(
            algorithms=[
                HnswAlgorithmConfiguration(
                    name="myHnsw"
                )
            ],
            profiles=[
                VectorSearchProfile(
                    name="myHnswProfile",
                    algorithm_configuration_name="myHnsw",
                    vectorizer="myVectorizer"
                )
            ],
            vectorizers=[
                AzureOpenAIVectorizer(
                    name="myVectorizer",
                    azure_open_ai_parameters=AzureOpenAIParameters(
                        resource_uri=OAI_API_ENDPOINT,
                        deployment_id=OAI_EMBED_DEPLOYMENT_NAME,
                        model_name=OAI_EMBED_DEPLOYMENT_NAME,
                        api_key=OAI_API_KEY
                    )
                )
            ]
        )

        semantic_config = SemanticConfiguration(
            name="my-semantic-config",
            prioritized_fields=SemanticPrioritizedFields(
                title_field=SemanticField(field_name="CaseID"),
                content_fields=[
                    SemanticField(field_name="Description"),
                    SemanticField(field_name="CustomerID"),
                    SemanticField(field_name="JobAssigned"),
                    SemanticField(field_name="DateOpened")
                ]
            )
        )

        # Create the semantic settings with the configuration
        semantic_search = SemanticSearch(configurations=[semantic_config])

        # Create the search index with the semantic settings
        index = SearchIndex(
            name=SEARCH_INDEX_NAME,
            fields=fields,
            vector_search=vector_search,
            semantic_search=semantic_search
        )

        result = index_client.create_or_update_index(index)
        logger.info(f'Search Index {result.name} created')
    except Exception as e:
        logger.error(f"An error occurred while creating the search index: {e}")


# Store data in Azure AI search index
def store_in_search_index(data):
    client = SearchClient(endpoint=SEARCH_SERVICE_ENDPOINT,
                          index_name=SEARCH_INDEX_NAME,
                          credential=AzureKeyCredential(SEARCH_API_KEY))
    
    try:
        client.upload_documents(documents=data)
        logger.info(f"Documents uploaded to search index {SEARCH_INDEX_NAME}.")
    except Exception as e:
        logger.error(f"Failed to upload documents to search index {SEARCH_INDEX_NAME}. Error: {e}")


async def main():
    pool = await create_pool()
    try:
        # Step 1: Create the container if it does not exist 
        await create_container_if_not_exists(container_client)
        
        # Step 2: Create the SQL table and insert dummy data
        await create_sql_table(pool)
        data_folder = "data/"
        await create_dummy_database(pool, data_folder)

        # Step 3: Create the search index
        create_search_index()

        # Step 4: Process images for indexing and create JSON file
        json_file_path = "scripts/indexdata.json"
        data = await process_cases_for_indexing(pool, json_file_path)

        # Step 5: Store data in search index
        store_in_search_index(data)
    finally:
        pool.close()
        await pool.wait_closed()
        await blob_service_client.close()

if __name__ == "__main__":
    asyncio.run(main())