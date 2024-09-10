import os
import base64
import json
import requests
import pyodbc
import random
import datetime
from openai import AzureOpenAI
from azure.core.credentials import AzureKeyCredential
from azure.storage.blob import BlobServiceClient, PublicAccess
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
from mimetypes import guess_type

# Debug environment variables
# print("Environment Variables:")
# for key, value in os.environ.items():
#    print(f"{key}: {value}")

# Configuration
OAI_API_ENDPOINT = os.getenv("AZURE_OAI_ENDPOINT")
OAI_API_KEY = os.getenv("AZURE_OAI_API_KEY")
OAI_EMBED_DEPLOYMENT_NAME = os.getenv("AZURE_OAI_EMBED_DEPLOYMENT_NAME") or "text-embedding-ada-002"
OAI_GPTVISION_DEPLOYMENT_NAME = os.getenv("AZURE_OAI_GPTVISION_DEPLOYMENT_NAME") or "gpt-4-turbo"
OAI_GPT4V_API_ENDPOINT = f"{os.getenv("AZURE_OAI_ENDPOINT")}openai/deployments/gpt-4-turbo/chat/completions?api-version=2024-02-15-preview"
SEARCH_SERVICE_ENDPOINT = os.getenv("AZURE_SEARCH_SERVICE_ENDPOINT")
SEARCH_INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX_NAME") or "maintenance-requests"
SEARCH_API_KEY = os.getenv("AZURE_SEARCH_API_KEY")
SEARCH_SERVICE_NAME = os.getenv("AZURE_SEARCH_SERVICE_NAME")
BLOB_CONNECTION_STRING = os.getenv("AZURE_BLOB_CONNECTION_STRING")
STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER") or "images"
SQL_SERVER = os.getenv("AZURE_SQL_SERVER") or ""
SQL_DATABASE = os.getenv("AZURE_SQL_DATABASE") or "maintenance-requests"
SQL_CONNECTION_STRING = os.getenv("AZURE_PYTHON_SQL_CONNECTION_STRING")

# Use DefaultAzureCredential for authentication
credential = DefaultAzureCredential()

# BloB Service Client
blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
container_client = blob_service_client.get_container_client(STORAGE_CONTAINER)

# Azure OpenAI Embed Client
embedClient = AzureOpenAI(
  api_key = OAI_API_KEY,  
  api_version = "2023-05-15",
  base_url=f"{OAI_API_ENDPOINT}openai/deployments/{OAI_EMBED_DEPLOYMENT_NAME}",
)

# SQL Authentication
def get_conn():
    try:
        conn = pyodbc.connect(SQL_CONNECTION_STRING)
        return conn
    except pyodbc.Error as e:
        print(f"Error occurred while connecting to SQL: {e}")
        return None

def generate_random_date_within_last_6_months():
    # Generate a random date within the last 6 months
    end_date = datetime.datetime.now(datetime.timezone.utc)
    start_date = end_date - datetime.timedelta(days=180)
    random_date = start_date + (end_date - start_date) * random.random()
    return random_date.isoformat(timespec='milliseconds').replace('+00:00', 'Z')  # Ensure UTC format

def generate_random_job_assigned():
    return random.choice(["yes", "no"])

# Function to create the Azure SQL table
def create_sql_table(conn):
    print("Starting to create the Azure SQL table 'MaintenanceRequests'.")
    try:
        cursor = conn.cursor()

        # Drop the table if it exists
        cursor.execute("""
        IF OBJECT_ID('dbo.MaintenanceRequests', 'U') IS NOT NULL
        DROP TABLE dbo.MaintenanceRequests
        """)
        print("Dropped existing table 'MaintenanceRequests' if it existed.")
        
        # Create the table
        cursor.execute("""
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
        conn.commit()
        print("Azure SQL table 'MaintenanceRequests' created successfully.")
    except pyodbc.Error as e:
        print(f"Error occurred while creating the SQL table: {e}")

# Function to insert data into the Azure SQL table
def insert_into_sql_table(conn, customer_id, case_id, description, image_url, mould_detected, file_name, date_opened, job_assigned):
    try:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO MaintenanceRequests (CustomerID, CaseID, Description, ImageURL, MouldDetected, FileName, DateOpened, JobAssigned)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (customer_id, case_id, description, image_url, mould_detected, file_name, date_opened, job_assigned))
        conn.commit()
    except Exception as e:
        print(f"An error occurred: {e}")

# Function to generate a description using GPT-4 Vision
def generate_image_description(image_data):

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
                        "text": "As an AI assistant for a housing association, your primary task is to provide a short description of the image, the repair thats needed, and the tradesman needed to complete the job. In addition, a key focus is the identification of mould which should be rated according to severity in all responses. Therefore, if mould is present in the image always add the words MOULD DETECTED. If no mould is present in the image always add the words MOULD NOT DETECTED. Your response should always follow the heading order and content of Image Description, Repair needed, Tradesman Required, and finally Mould Status. Always in that order, no exceptions. Remember to provide accurate and concise answers based on the information present in the image and use external knowledge of building maintenance. Your response should not provide a request for more info as this info will be injected into an AI Search index field."
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
    try:
        response = requests.post(OAI_GPT4V_API_ENDPOINT, headers=headers, json=payload)
        response.raise_for_status()
        #response_json = response.json()
        #print("Response JSON:", response_json)  # Debugging line to print the response
        description = response.json()['choices'][0]['message']['content']
        return description
    except requests.RequestException as e:
        raise SystemExit(f"Failed to make the request. Error: {e}")
    except (KeyError, IndexError, TypeError) as e:
        raise SystemExit(f"Unexpected response format: {e}")

# Function to update descriptions in SQL table using GPT-4 Vision
def update_descriptions_in_sql(conn):
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT CaseID, ImageURL FROM MaintenanceRequests WHERE Description = ''")
        rows = cursor.fetchall()
        for row in rows:
            case_id, image_url = row
            description = generate_image_description(image_url)
            mould_detected = detect_mould_status(description)
            cursor.execute("""
            UPDATE MaintenanceRequests
            SET Description = ?, MouldDetected = ?
            WHERE CaseID = ?
            """, (description, mould_detected, case_id))
        conn.commit()
    except Exception as e:
        print(f"An error occurred: {e}")

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
        print(f"An error occurred while detecting mould status: {e}")
        return False

# Function to generate vector representation using Azure OpenAI embedding model
def generate_vector(description):
    try:
        response = embedClient.embeddings.create(
            input=description,
            model=OAI_EMBED_DEPLOYMENT_NAME 
        )
        vector = response.data[0].embedding
        return vector
    except Exception as e:
        print(f"An error occurred while generating the vector for the description: {e}")
        return None

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
                print(f"Recreating search index {SEARCH_INDEX_NAME}")
                index_client.delete_index(SEARCH_INDEX_NAME)
                print(f"Search Index {SEARCH_INDEX_NAME} deleted")
            except Exception as e:
                print(f"An error occurred while deleting the search index {SEARCH_INDEX_NAME}: {e}")
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
        print(f'Search Index {result.name} created')
    except Exception as e:
        print(f"An error occurred while creating the search index: {e}")

# Function to store data in Azure AI search index
def store_in_search_index(data):
    client = SearchClient(endpoint=SEARCH_SERVICE_ENDPOINT,
                          index_name=SEARCH_INDEX_NAME,
                          credential=AzureKeyCredential(SEARCH_API_KEY))
    
    try:
        client.upload_documents(documents=data)
        print(f"Documents uploaded to search index {SEARCH_INDEX_NAME}.")
    except Exception as e:
        print(f"Failed to upload documents to search index {SEARCH_INDEX_NAME}. Error: {e}")

# Function to create the container if it does not exist
def create_container_if_not_exists(container_client):
    try:
        if not container_client.exists():
            container_client.create_container()
            print(f"Container '{STORAGE_CONTAINER}' created.")
        else:
            print(f"Container '{STORAGE_CONTAINER}' already exists.")
    except Exception as e:
        print(f"An error occurred while checking/creating the container: {e}")


# Function to upload image to Azure Blob Storage and get the URL
def upload_image_to_blob(image_path, filename):
    try:
        blob_client = container_client.get_blob_client(filename)
        if blob_client.exists():
            print(f"Image {filename} already exists in blob storage.")
            return blob_client.url
        else:
            with open(image_path, "rb") as data:
                blob_client.upload_blob(data, overwrite=True)
            return blob_client.url
    except Exception as e:
        print(f"An error occurred while uploading {filename} to blob storage: {e}")
        return None

# Function to process the images and insert dummy data into SQL table
def create_dummy_database(conn, data_folder):
    try:
        for filename in os.listdir(data_folder):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp')):
                image_path = os.path.join(data_folder, filename)
                
                try:
                    # Upload image to Azure Blob Storage and get the URL
                    image_url = upload_image_to_blob(image_path, filename)

                    # Generate dummy data for CustomerID and CaseID
                    customer_id = str(random.randint(1000, 9999))
                    case_id = str(random.randint(100000, 999999))
                    date_opened = generate_random_date_within_last_6_months()
                    job_assigned = generate_random_job_assigned()

                    # Insert data into SQL table
                    insert_into_sql_table(conn, customer_id, case_id, "", image_url, False, filename, date_opened, job_assigned)

                    print(f"Processed {filename}")
                except Exception as e:
                    print(f"An error occurred while processing {filename}: {e}")
                    continue  # Skip to the next file if there's an error
    except Exception as e:
        print(f"An error occurred while accessing the data folder {data_folder}: {e}")

# Processes cases for indexing adding GenAI generated descriptions. Outputs a JSON file with the index data.    
def process_cases_for_indexing(conn, json_file_path):
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM MaintenanceRequests")
        rows = cursor.fetchall()
        data = []
        for row in rows:
            customer_id, case_id, description, image_url, mould_detected, file_name, date_opened, job_assigned = row
            if not description:
                blob_name = image_url.split('/')[-1]
                blob_client = container_client.get_blob_client(blob=blob_name)
                image_data = blob_client.download_blob().readall()
                blob_base64 = base64.b64encode(image_data).decode('utf-8')
                description = generate_image_description(blob_base64)
                mould_detected = detect_mould_status(description)
                cursor.execute("""
                UPDATE MaintenanceRequests
                SET Description = ?, MouldDetected = ?
                WHERE CaseID = ?
                """, (description, mould_detected, case_id))
                conn.commit()
            vector = generate_vector(description)
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
            print(f"Processed {case_id} for indexing")

        # Write data to JSON file
        with open(json_file_path, 'w') as json_file:
            json.dump(data, json_file, indent=4)
        print(f"JSON file {json_file_path} created successfully.")
        return data
    except Exception as e:
        print(f"An error occurred: {e}")
        return None

def main():
    conn = get_conn()
    try:

        # Step 0: Create the container if it does not exist 
        create_container_if_not_exists(container_client)
        
        # Step 1: Create the SQL table and insert dummy data
        create_sql_table(conn)
        data_folder = "data/"
        create_dummy_database(conn, data_folder)

        # Step 2: Create the search index
        create_search_index()

        # Step 3: Process images for indexing and create JSON file
        json_file_path = "indexdata.json"
        data = process_cases_for_indexing(conn, json_file_path)

        # Step 4: Store data in search index
        store_in_search_index(data)
    finally:
        conn.close()

if __name__ == "__main__":
    main()