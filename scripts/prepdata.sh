 #!/bin/sh

echo ""
echo "Loading azd .env file from current environment"
echo ""

while IFS='=' read -r key value; do
    value=$(echo "$value" | sed 's/^"//' | sed 's/"$//')
    export "$key=$value"
done <<EOF
$(azd env get-values)
EOF

echo 'Installing ODBC driver'
# Debian 12
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg

#Download appropriate package for the OS version
#Choose only ONE of the following, corresponding to your OS version

#Debian 12
curl https://packages.microsoft.com/config/debian/12/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list

sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18
# optional: for bcp and sqlcmd
sudo ACCEPT_EULA=Y apt-get install -y mssql-tools18
echo 'export PATH="$PATH:/opt/mssql-tools18/bin"' >> ~/.bashrc
source ~/.bashrc
# optional: for unixODBC development headers
sudo apt-get install -y unixodbc-dev


echo 'Creating python virtual environment "scripts/scripts_env"'
python3 -m venv scripts/scripts_env

echo 'Installing dependencies from "requirements.txt" into virtual environment'
./scripts/scripts_env/bin/python -m pip install -r scripts/requirements.txt

echo 'Running "prepdata.py"'
./scripts/scripts_env/bin/python ./scripts/prepdata.py