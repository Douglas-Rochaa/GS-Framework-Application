from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import oracledb
import os
from datetime import datetime
from dotenv import load_dotenv

# Carregar variáveis de ambiente do arquivo .env (para desenvolvimento local)
load_dotenv()

app = FastAPI(
    title="Recomeçar API com Oracle",
    description="API para ajudar pessoas em situação de enchente, conectada ao Oracle.",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuração do Banco de Dados Oracle a partir de variáveis de ambiente
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "1521")
DB_SERVICE_NAME = os.getenv("DB_SERVICE_NAME")

# Validação inicial das variáveis de ambiente do banco
missing_vars = [var for var in [DB_USER, DB_PASSWORD, DB_HOST, DB_SERVICE_NAME] if var is None]
if missing_vars:
    # Não lance erro na inicialização para permitir o deploy no Render sem as vars inicialmente
    # O erro ocorrerá ao tentar conectar se as vars não estiverem configuradas no Render.
    print(f"AVISO: Variáveis de ambiente do banco não configuradas: {', '.join(var_name for var_name, var_val in [('DB_USER', DB_USER), ('DB_PASSWORD', '***'), ('DB_HOST', DB_HOST), ('DB_SERVICE_NAME', DB_SERVICE_NAME)] if var_val is None)}")
    # raise ValueError(f"Variáveis de ambiente do banco não configuradas: {', '.join(missing_vars)}")

# Construir DSN (Data Source Name) para cx_Oracle
# Exemplo: "oracle.fiap.com.br:1521/orcl.fiap.com.br"
DB_DSN = f"{DB_HOST}:{DB_PORT}/{DB_SERVICE_NAME}" if DB_HOST and DB_SERVICE_NAME else None

# Pool de Conexões (Recomendado para produção)
pool = None

def init_oracle_pool():
    global pool
    if not all([DB_USER, DB_PASSWORD, DB_DSN]):
        print("Não é possível inicializar o pool do Oracle: credenciais ou DSN ausentes.")
        return

    try:
        print(f"Tentando inicializar pool Oracle com DSN: {DB_DSN}")
        pool = oracledb.SessionPool(user=DB_USER, password=DB_PASSWORD, dsn=DB_DSN,
                                     min=2, max=5, increment=1, encoding="UTF-8")
        print("Pool de conexões Oracle inicializado com sucesso.")
    except oracledb.Error as e:
        print(f"Erro ao inicializar pool Oracle: {e}")
        pool = None # Garante que o pool não seja usado se a inicialização falhar

@app.on_event("startup")
async def startup_event():
    init_oracle_pool()

@app.on_event("shutdown")
async def shutdown_event():
    global pool
    if pool:
        pool.close()
        print("Pool de conexões Oracle fechado.")

# Função para obter conexão do pool
def get_db_connection():
    global pool
    if not pool:
        # Tenta reinicializar se o pool não estiver disponível (pode ter falhado no startup)
        print("Pool não inicializado. Tentando inicializar agora...")
        init_oracle_pool()
        if not pool: # Se ainda assim falhar
             raise HTTPException(status_code=503, detail="Serviço de banco de dados indisponível (pool não inicializado). Verifique as configurações e logs do servidor.")

    try:
        # Adquire uma conexão do pool
        # O parâmetro 'timeout' pode ser útil aqui em cenários de alta carga
        conn = pool.acquire()
        return conn
    except oracledb.Error as e:
        print(f"Erro ao adquirir conexão do pool: {e}")
        raise HTTPException(status_code=503, detail=f"Erro ao conectar ao banco de dados: {e}")


# Helper para executar queries e retornar resultados como dicts
def execute_query(query: str, params: Optional[Dict[str, Any]] = None, fetch_one: bool = False, commit: bool = False, is_ddl: bool = False):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Para DML/DDL que retorna ID (usando RETURNING INTO)
        if "RETURNING" in query.upper() and params:
            # Criar variáveis de bind para os out_params
            out_params = {key: cursor.var(oracledb.NUMBER) for key in params if key.startswith("out_")}
            bind_params = {**params, **out_params}
            cursor.execute(query, bind_params)
            returned_ids = {key: var.getvalue()[0] for key, var in out_params.items()} # getvalue() retorna lista
            if commit:
                conn.commit()
            return returned_ids
        
        cursor.execute(query, params or {})

        if is_ddl or commit: # DDL é auto-commit em algumas configs, mas explícito é melhor. Commit para DML.
            conn.commit()
            return None # DDL ou commit DML não retorna linhas

        if fetch_one:
            row = cursor.fetchone()
            if row:
                columns = [col[0].lower() for col in cursor.description]
                return dict(zip(columns, row))
            return None
        else:
            rows = cursor.fetchall()
            columns = [col[0].lower() for col in cursor.description]
            return [dict(zip(columns, row)] for row in rows]
            
    except oracledb.DatabaseError as e:
        error_obj, = e.args
        print(f"Oracle Database Error: {error_obj.code} - {error_obj.message}")
        # Se for erro de "not logged on", pode ser que o pool perdeu a conexão
        if error_obj.code == 24324 or error_obj.code == 1033 or error_obj.code == 1089 or error_obj.code == 3113 or error_obj.code == 3114 or error_obj.code == 12537 or error_obj.code == 12541:
             print("Erro de conexão detectado. Tentando fechar e reabrir o pool.")
             global pool
             if pool:
                 try:
                     pool.close()
                 except Exception as close_err:
                     print(f"Erro ao fechar pool existente: {close_err}")
                 pool = None
             init_oracle_pool() # Tenta reestabelecer
        raise HTTPException(status_code=500, detail=f"Erro no banco de dados Oracle: {error_obj.message}")
    except Exception as e:
        print(f"Erro genérico na execução da query: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno do servidor: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            pool.release(conn) # Libera a conexão de volta para o pool

# --- Models Pydantic (semelhantes aos anteriores, mas ID pode ser opcional na criação) ---
class PessoaBase(BaseModel):
    nome: str
    cpf: str
    telefone: Optional[str] = None
    endereco: Optional[str] = None
    situacao: str
    necessidades: Optional[str] = None

class PessoaCreate(PessoaBase):
    pass

class PessoaUpdate(BaseModel):
    nome: Optional[str] = None
    telefone: Optional[str] = None
    endereco: Optional[str] = None
    situacao: Optional[str] = None
    necessidades: Optional[str] = None

class Pessoa(PessoaBase):
    id_pessoa: int # Nome da coluna no Oracle
    data_cadastro: datetime

# ... (Defina AbrigoBase, AbrigoCreate, AbrigoUpdate, Abrigo de forma similar)
class AbrigoBase(BaseModel):
    nome: str
    endereco: str
    capacidade: int
    ocupacao_atual: Optional[int] = 0
    responsavel: Optional[str] = None
    telefone_responsavel: Optional[str] = None # Corrigido nome do campo
    recursos_disponiveis: Optional[str] = None

class AbrigoCreate(AbrigoBase):
    pass

class AbrigoUpdate(BaseModel):
    nome: Optional[str] = None
    endereco: Optional[str] = None
    capacidade: Optional[int] = None
    ocupacao_atual: Optional[int] = None
    responsavel: Optional[str] = None
    telefone_responsavel: Optional[str] = None
    recursos_disponiveis: Optional[str] = None

class Abrigo(AbrigoBase):
    id_abrigo: int
    data_criacao: datetime


# ... (Defina DoacaoBase, DoacaoCreate, DoacaoUpdate, Doacao de forma similar)
class DoacaoBase(BaseModel):
    doador_nome: Optional[str] = None
    doador_telefone: Optional[str] = None
    tipo_doacao: str
    descricao: str
    quantidade: Optional[str] = None
    status: str
    id_abrigo_destino: Optional[int] = None

class DoacaoCreate(DoacaoBase):
    pass

class DoacaoUpdate(BaseModel):
    doador_nome: Optional[str] = None
    doador_telefone: Optional[str] = None
    tipo_doacao: Optional[str] = None
    descricao: Optional[str] = None
    quantidade: Optional[str] = None
    status: Optional[str] = None
    id_abrigo_destino: Optional[str] = None

class Doacao(DoacaoBase):
    id_doacao: int
    data_doacao: datetime


# --- Rotas API com Oracle ---

# Pessoas
@app.post("/pessoas", response_model=Pessoa, status_code=201)
async def cadastrar_pessoa(pessoa: PessoaCreate):
    query = """
        INSERT INTO PESSOAS (NOME, CPF, TELEFONE, ENDERECO, SITUACAO, NECESSIDADES)
        VALUES (:nome, :cpf, :telefone, :endereco, :situacao, :necessidades)
        RETURNING ID_PESSOA INTO :out_id_pessoa
    """
    params = pessoa.dict()
    params["out_id_pessoa"] = None # Placeholder para o valor retornado
    
    try:
        returned_data = execute_query(query, params, commit=True)
        new_id = returned_data['out_id_pessoa']
        # Buscar o registro recém-criado para retornar todos os campos, incluindo data_cadastro default
        return await obter_pessoa(new_id)
    except HTTPException as e:
        if "UNIQUE_CONSTRAINT_VIOLATED" in str(e.detail).upper() or "ORA-00001" in str(e.detail): # ORA-00001 é unique constraint
            raise HTTPException(status_code=409, detail=f"CPF {pessoa.cpf} já cadastrado.")
        raise e


@app.get("/pessoas", response_model=List[Pessoa])
async def listar_pessoas():
    query = "SELECT ID_PESSOA, NOME, CPF, TELEFONE, ENDERECO, SITUACAO, NECESSIDADES, DATA_CADASTRO FROM PESSOAS ORDER BY NOME"
    result = execute_query(query)
    return result

@app.get("/pessoas/{pessoa_id}", response_model=Pessoa)
async def obter_pessoa(pessoa_id: int):
    query = "SELECT ID_PESSOA, NOME, CPF, TELEFONE, ENDERECO, SITUACAO, NECESSIDADES, DATA_CADASTRO FROM PESSOAS WHERE ID_PESSOA = :id_pessoa"
    pessoa = execute_query(query, {"id_pessoa": pessoa_id}, fetch_one=True)
    if not pessoa:
        raise HTTPException(status_code=404, detail="Pessoa não encontrada")
    return pessoa

@app.put("/pessoas/{pessoa_id}", response_model=Pessoa)
async def atualizar_pessoa(pessoa_id: int, pessoa_update: PessoaUpdate):
    # Primeiro, verifica se a pessoa existe
    await obter_pessoa(pessoa_id) # Isso lançará 404 se não existir

    update_data = pessoa_update.dict(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="Nenhum dado fornecido para atualização")

    set_clauses = ", ".join([f"{key.upper()} = :{key}" for key in update_data.keys()])
    query = f"UPDATE PESSOAS SET {set_clauses} WHERE ID_PESSOA = :id_pessoa_param"
    
    params = {**update_data, "id_pessoa_param": pessoa_id}
    
    execute_query(query, params, commit=True)
    return await obter_pessoa(pessoa_id)

@app.delete("/pessoas/{pessoa_id}", status_code=204)
async def deletar_pessoa(pessoa_id: int):
    # Primeiro, verifica se a pessoa existe
    await obter_pessoa(pessoa_id) # Isso lançará 404 se não existir
    query = "DELETE FROM PESSOAS WHERE ID_PESSOA = :id_pessoa"
    execute_query(query, {"id_pessoa": pessoa_id}, commit=True)
    return None # HTTP 204 No Content

# --- Rotas para Abrigos (implementar de forma similar) ---
@app.post("/abrigos", response_model=Abrigo, status_code=201)
async def cadastrar_abrigo(abrigo: AbrigoCreate):
    query = """
        INSERT INTO ABRIGOS (NOME, ENDERECO, CAPACIDADE, OCUPACAO_ATUAL, RESPONSAVEL, TELEFONE_RESPONSAVEL, RECURSOS_DISPONIVEIS)
        VALUES (:nome, :endereco, :capacidade, :ocupacao_atual, :responsavel, :telefone_responsavel, :recursos_disponiveis)
        RETURNING ID_ABRIGO INTO :out_id_abrigo
    """
    params = abrigo.dict()
    params["out_id_abrigo"] = None
    try:
        returned_data = execute_query(query, params, commit=True)
        new_id = returned_data['out_id_abrigo']
        return await obter_abrigo(new_id)
    except HTTPException as e:
        raise e

@app.get("/abrigos", response_model=List[Abrigo])
async def listar_abrigos():
    query = "SELECT ID_ABRIGO, NOME, ENDERECO, CAPACIDADE, OCUPACAO_ATUAL, RESPONSAVEL, TELEFONE_RESPONSAVEL, RECURSOS_DISPONIVEIS, DATA_CRIACAO FROM ABRIGOS ORDER BY NOME"
    return execute_query(query)

@app.get("/abrigos/{abrigo_id}", response_model=Abrigo)
async def obter_abrigo(abrigo_id: int):
    query = "SELECT ID_ABRIGO, NOME, ENDERECO, CAPACIDADE, OCUPACAO_ATUAL, RESPONSAVEL, TELEFONE_RESPONSAVEL, RECURSOS_DISPONIVEIS, DATA_CRIACAO FROM ABRIGOS WHERE ID_ABRIGO = :id_abrigo"
    abrigo_data = execute_query(query, {"id_abrigo": abrigo_id}, fetch_one=True)
    if not abrigo_data:
        raise HTTPException(status_code=404, detail="Abrigo não encontrado")
    return abrigo_data

@app.put("/abrigos/{abrigo_id}", response_model=Abrigo)
async def atualizar_abrigo(abrigo_id: int, abrigo_update: AbrigoUpdate):
    await obter_abrigo(abrigo_id) # Valida existência
    update_data = abrigo_update.dict(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="Nenhum dado para atualização")
    set_clauses = ", ".join([f"{key.upper()} = :{key}" for key in update_data.keys()])
    query = f"UPDATE ABRIGOS SET {set_clauses} WHERE ID_ABRIGO = :id_abrigo_param"
    params = {**update_data, "id_abrigo_param": abrigo_id}
    execute_query(query, params, commit=True)
    return await obter_abrigo(abrigo_id)

@app.delete("/abrigos/{abrigo_id}", status_code=204)
async def deletar_abrigo(abrigo_id: int):
    await obter_abrigo(abrigo_id) # Valida existência
    # Adicionar lógica para verificar se o abrigo tem dependências (pessoas, doações) antes de excluir, se necessário
    query = "DELETE FROM ABRIGOS WHERE ID_ABRIGO = :id_abrigo"
    execute_query(query, {"id_abrigo": abrigo_id}, commit=True)
    return None

# --- Rotas para Doações (implementar de forma similar) ---
@app.post("/doacoes", response_model=Doacao, status_code=201)
async def cadastrar_doacao(doacao: DoacaoCreate):
    query = """
        INSERT INTO DOACOES (DOADOR_NOME, DOADOR_TELEFONE, TIPO_DOACAO, DESCRICAO, QUANTIDADE, STATUS, ID_ABRIGO_DESTINO)
        VALUES (:doador_nome, :doador_telefone, :tipo_doacao, :descricao, :quantidade, :status, :id_abrigo_destino)
        RETURNING ID_DOACAO INTO :out_id_doacao
    """
    params = doacao.dict()
    params["out_id_doacao"] = None
    try:
        returned_data = execute_query(query, params, commit=True)
        new_id = returned_data['out_id_doacao']
        return await obter_doacao(new_id)
    except HTTPException as e:
        raise e

@app.get("/doacoes", response_model=List[Doacao])
async def listar_doacoes():
    query = "SELECT ID_DOACAO, DOADOR_NOME, DOADOR_TELEFONE, TIPO_DOACAO, DESCRICAO, QUANTIDADE, STATUS, DATA_DOACAO, ID_ABRIGO_DESTINO FROM DOACOES ORDER BY DATA_DOACAO DESC"
    return execute_query(query)

@app.get("/doacoes/{doacao_id}", response_model=Doacao)
async def obter_doacao(doacao_id: int):
    query = "SELECT ID_DOACAO, DOADOR_NOME, DOADOR_TELEFONE, TIPO_DOACAO, DESCRICAO, QUANTIDADE, STATUS, DATA_DOACAO, ID_ABRIGO_DESTINO FROM DOACOES WHERE ID_DOACAO = :id_doacao"
    doacao_data = execute_query(query, {"id_doacao": doacao_id}, fetch_one=True)
    if not doacao_data:
        raise HTTPException(status_code=404, detail="Doação não encontrada")
    return doacao_data

@app.put("/doacoes/{doacao_id}", response_model=Doacao)
async def atualizar_doacao(doacao_id: int, doacao_update: DoacaoUpdate):
    await obter_doacao(doacao_id) # Valida existência
    update_data = doacao_update.dict(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="Nenhum dado para atualização")
    set_clauses = ", ".join([f"{key.upper()} = :{key}" for key in update_data.keys()])
    query = f"UPDATE DOACOES SET {set_clauses} WHERE ID_DOACAO = :id_doacao_param"
    params = {**update_data, "id_doacao_param": doacao_id}
    execute_query(query, params, commit=True)
    return await obter_doacao(doacao_id)

@app.delete("/doacoes/{doacao_id}", status_code=204)
async def deletar_doacao(doacao_id: int):
    await obter_doacao(doacao_id) # Valida existência
    query = "DELETE FROM DOACOES WHERE ID_DOACAO = :id_doacao"
    execute_query(query, {"id_doacao": doacao_id}, commit=True)
    return None

# Rota de Estatísticas (adaptar para Oracle)
@app.get("/estatisticas")
async def obter_estatisticas():
    query_pessoas_total = "SELECT COUNT(*) AS total FROM PESSOAS"
    query_pessoas_desabrigadas = "SELECT COUNT(*) AS total FROM PESSOAS WHERE SITUACAO = 'desabrigado'"
    query_abrigos_total = "SELECT COUNT(*) AS total FROM ABRIGOS"
    query_vagas_disponiveis = "SELECT SUM(CAPACIDADE - OCUPACAO_ATUAL) AS total FROM ABRIGOS"
    query_doacoes_total = "SELECT COUNT(*) AS total FROM DOACOES"
    query_doacoes_pendentes = "SELECT COUNT(*) AS total FROM DOACOES WHERE STATUS = 'pendente'"

    try:
        total_pessoas = execute_query(query_pessoas_total, fetch_one=True)['total']
        pessoas_desabrigadas = execute_query(query_pessoas_desabrigadas, fetch_one=True)['total']
        total_abrigos = execute_query(query_abrigos_total, fetch_one=True)['total']
        vagas_disponiveis_result = execute_query(query_vagas_disponiveis, fetch_one=True)
        vagas_disponiveis = vagas_disponiveis_result['total'] if vagas_disponiveis_result and vagas_disponiveis_result['total'] is not None else 0
        total_doacoes = execute_query(query_doacoes_total, fetch_one=True)['total']
        doacoes_pendentes = execute_query(query_doacoes_pendentes, fetch_one=True)['total']
    except Exception as e:
        print(f"Erro ao buscar estatísticas: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao buscar estatísticas do banco: {e}")

    return {
        "total_pessoas": total_pessoas,
        "pessoas_desabrigadas": pessoas_desabrigadas,
        "total_abrigos": total_abrigos,
        "vagas_disponiveis": vagas_disponiveis,
        "total_doacoes": total_doacoes,
        "doacoes_pendentes": doacoes_pendentes
    }

if __name__ == "__main__":
    import uvicorn
    # Verifica se as variáveis de ambiente essenciais para o pool estão definidas antes de tentar rodar
    if not all([DB_USER, DB_PASSWORD, DB_DSN]):
        print("ERRO FATAL: Variáveis de ambiente para conexão com Oracle não estão definidas (DB_USER, DB_PASSWORD, DB_HOST, DB_SERVICE_NAME).")
        print("Verifique seu arquivo .env ou as configurações de ambiente do servidor.")
    else:
         uvicorn.run(app, host="0.0.0.0", port=8000)