import hashlib
import os
import time  # 소요 시간 측정을 위한 모듈 임포트
import dotenv
from sentence_transformers import SentenceTransformer
from huggingface_hub import login
from ocr_service import test_ocr

# SQLAlchemy 및 pgvector 관련 임포트
from sqlalchemy import create_engine, Column, Integer, String, Text,ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker
from pgvector.sqlalchemy import Vector
from model_service import call_ollama

# 환경 변수 로드 및 모델 초기화
dotenv.load_dotenv()
login(token=os.getenv("HF_TOKEN"))

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
UPLOAD_DIR = os.path.join("dcm-back", "uploads")

# 1. SQLAlchemy 엔진 및 세션 설정
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")

DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}"
engine = None
if engine is None:
    engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# 2. ORM 모델 정의 (기존 테이블 구조 매핑)
class DocuModel(Base):
    __tablename__ = "docu_t"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String)
    content = Column(Text)
    embed = Column(Vector(384))  
    filehash = Column(String, unique=True, index=True)


class QueryModel(Base):
    __tablename__ = "query_t"

    id = Column(Integer, primary_key=True, index=True)
    origin_query = Column(Text)
    embed_query = Column(Vector(384))
    query_answer = Column(Text)
    docu_id = Column(Integer, ForeignKey(DocuModel.id))


def calculate_file_hash(file_path: str) -> str:
    """파일의 SHA-256 해시값을 계산합니다."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def save_docu(model,filenames: list[str]):
    session = SessionLocal()
    try:
        for filename in filenames:
            path = os.path.join(UPLOAD_DIR, filename)
            
            if not os.path.exists(path):
                print(f"파일을 찾을 수 없습니다: {path}")
                continue

            print(f"🔍 [1/4] 파일 해시 계산 중: {filename}")
            file_hash = calculate_file_hash(path)

            existing_doc = session.query(DocuModel).filter_by(filehash=file_hash).first()
            if existing_doc:
                print(f"[스킵] 이미 존재하는 파일입니다: {filename}")
                continue

            print(f"[2/4] 텍스트 변환 수행 중 (OCR일 경우 시간이 오래 걸립니다.): {filename}")

            docu_text =""

            extention = filename.split(".")[-1]
            if(extention == "txt"):
                with open(path, 'r', encoding='utf-8') as file:
                    docu_text = file.read()
            else:
                docu_text = test_ocr(path)
            print(f"텍스트 변환 완료! 추출된 텍스트 길이: {len(docu_text)}글자")

            print(f"[3/4] 임베딩 모델로 벡터 변환 중...")
            docu_embeddings = model.encode([docu_text])
            docu_emb_list = docu_embeddings[0].tolist()

            print(f"[4/4] 데이터베이스 저장 중...")
            new_doc = DocuModel(
                filename=filename,
                content=docu_text,
                embed=docu_emb_list,
                filehash=file_hash,
            )
            session.add(new_doc)

        session.commit()
        print("모든 문서 저장 작업이 완료되었습니다!")
    except Exception as e:
        session.rollback()
        print(f"문서 저장 중 오류 발생: {e}")
        raise
    finally:
        session.close()


def querying(model, queries: list[str]):
    """사용자 쿼리를 저장하고, SQLAlchemy와 pgvector를 이용해 가장 유사한 문서를 검색합니다."""
    
    start_time = time.time()

    # 1. 쿼리 임베딩 생성
    query_embeddings = model.encode(queries, prompt_name="query")
    query_emb_list = query_embeddings[0].tolist()
    origin_query = " ".join(queries)

    session = SessionLocal()
    try:
        # 코사인 거리 연산을 활용한 상위 3개 문서 검색
        similarity_expr = 1 - DocuModel.embed.cosine_distance(query_emb_list)
        
        results = session.query(
            DocuModel.id,
            DocuModel.filename,
            DocuModel.content,
            similarity_expr.label("cosine_similarity")
        ).order_by(
            DocuModel.embed.cosine_distance(query_emb_list)
        ).limit(3).all()

        if not results:
            print("검색된 문서가 없습니다.")
            return

        # 2. 쿼리 기록 객체 생성 및 DB 반영 (id 자동 생성)
        new_query = QueryModel(
            origin_query=origin_query,
            embed_query=query_emb_list,
            docu_id=results[0][0]  # 가장 유사도가 높은 문서의 id
        )
        session.add(new_query)
        
        # flush() 또는 commit()을 수행하면 new_query.id에 값이 자동으로 채워집니다.
        session.flush() 

        end_time = time.time()
        elapsed_time = end_time - start_time

        # 3. 검색 결과 출력 및 컨텐츠 수집
        print("\n=== 검색 결과 ===")
        print(f"임베딩 모델: {EMBEDDING_MODEL_NAME}")
        contents = []
        # SELECT 칼럼 순서와 unpack 갯수 맞춤 (id, filename, content, similarity)
        for i, (doc_id, filename, content, similarity) in enumerate(results, start=1):
            print(f"Top {i}: {filename} (유사도: {similarity:.4f})")
            contents.append(content)
        
        print(f"\n 쿼리 총 소요 시간: {elapsed_time:.4f}초")

        # 4. LLM 호출
        result = call_ollama(question=origin_query, docu=" ".join(contents))
        
        # 5. LLM 답변을 new_query에 업데이트 및 최종 커밋
        new_query.query_answer = result
        session.commit()

        end_time2 = time.time()
        elapsed_time2 = end_time2 - end_time

        print(f"\n LLM 추론 소요 시간: {elapsed_time2:.4f}초")
        print(f"LLM 모델 답변: {result}")

    except Exception as e:
        session.rollback()
        print(f"쿼리 검색 중 오류 발생: {e}")
        raise
    finally:
        session.close()


# 실행 예시
if __name__ == "__main__":
    model = SentenceTransformer(EMBEDDING_MODEL_NAME, device="cpu")
    save_docu(model,filenames=["complaints_common.txt"])
    queries = ["테일러스인데 사면이 2종시설물에 해당할때 토사사면, 연약암반사면, 파쇄암반사면, 절리암반사면 중 어느것으로 평가해야 하나요?"]
    querying(model,queries=queries)

