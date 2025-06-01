#Use uma imagem base Python,
FROM python:3.9-slim

#Defina o diretório de trabalho,
WORKDIR /app

#Variáveis de ambiente para o Oracle Instant Client,
ENV LD_LIBRARY_PATH=/opt/oracle/instantclient_23_8
ENV ORACLE_HOME=/opt/oracle/instantclient_23_8

#Instale dependências do sistema,
RUN apt-get update && apt-get install -y unzip libaio1 wget \
    && rm -rf /var/lib/apt/lists/*

#Baixe e instale o Oracle Instant Client,
RUN mkdir -p /opt/oracle \
    # Baixe os arquivos do Oracle Instant Client diretamente
    && wget https://download.oracle.com/otn_software/linux/instantclient/211000/instantclient-basiclite-linux.x64-21.1.0.0.0.zip \
    && wget https://download.oracle.com/otn_software/linux/instantclient/211000/instantclient-sdk-linux.x64-21.1.0.0.0.zip \
    # Extraia os arquivos
    && unzip instantclient-basiclite-linux.x64-21.1.0.0.0.zip -d /opt/oracle \
    && unzip instantclient-sdk-linux.x64-21.1.0.0.0.zip -d /opt/oracle \
    # Limpe os arquivos ZIP
    && rm *.zip \
    # Configure o LD_LIBRARY_PATH
    && echo /opt/oracle/instantclient_21_1 > /etc/ld.so.conf.d/oracle-instantclient.conf \
    && ldconfig

#Copie os arquivos da aplicação,
COPY requirements.txt .
COPY main.py .

#Instale as dependências Python,
RUN pip install --no-cache-dir -r requirements.txt

#Comando para iniciar a aplicação,
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]