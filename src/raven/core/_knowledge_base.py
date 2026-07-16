import json
import sqlite3, sqlite_vec
import numpy as np
import os
import uuid
import logging
from llama_cpp import Llama
from sqlite3 import Cursor

from raven.core.constants import CHUNK_SIZE, CHUNK_OVERLAP, KNOWLEDGE_BASE_DIR

logger = logging.getLogger(__name__)

class Knowledge:
    """Knowledge database that contains multiple files"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)

                cursor = conn.cursor()
                cursor.execute("PRAGMA foreign_keys = ON;")
                
                # 1. File Table: Stores global file metadata for Stage 1 model evaluation
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS file_metadata (
                        file_id TEXT PRIMARY KEY,
                        file_name TEXT NOT NULL,
                        metadata TEXT NOT NULL          -- Full file JSON (all sections, no text)
                    );
                """)
                
                # 2. Section Table: Optimized for direct, fast Stage 2 text point-lookups
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS section_metadata (
                        file_id TEXT NOT NULL,
                        section_id TEXT PRIMARY KEY,
                        content_json TEXT NOT NULL,      -- Just the raw_content for this specific ID
                        FOREIGN KEY (file_id) REFERENCES file_metadata (file_id) ON DELETE CASCADE
                    );
                """)

                # 3. Chunk Table: Contains text chunks split form the raw content
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS chunk_metadata (
                        chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        section_id TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        chunk_text TEXT NOT NULL,
                        FOREIGN KEY (section_id) REFERENCES section_metadata (section_id) ON DELETE CASCADE
                    );
                """)

                # 4. Vector Table: stores Embeddings corresponding to each chunk
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                        chunk_id INTEGER PRIMARY KEY,
                        embedding FLOAT[1024]
                    );
                """)

                # 5. User summary for the entire database
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS knowledge_summary (
                        type TEXT PRIMARY KEY,
                        summary_text TEXT
                    );
                """)

                conn.commit()

                logger.info(f"Knowledge initialized: {db_path}")
            
        except Exception as e:
            logger.error(f"Failed to initialize Knowledge at {db_path}: {e}")
            raise


    def set_summary(self, summary_text: str) -> None:
        """Only call this when you actually have a summary to save."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO knowledge_summary (type, summary_text) VALUES (?, ?)", 
                ("user_summary", summary_text)
            )
            conn.commit()


    def _split_by_tokens(self, text: str, embedder: Llama, chunk_size: int = 150, overlap: int = 20) -> list[str]:
        token_ids = embedder.tokenize(text.encode("utf-8"))

        chunks = []
        start_idx = 0
        total_tokens = len(token_ids)

        while start_idx < total_tokens:
            end_idx = min(start_idx + chunk_size, total_tokens)
        
            # Pull out the exact slice of token IDs
            token_slice = token_ids[start_idx:end_idx]
            
            # 3. Convert the token ID slice back into a clean Python string
            chunk_text = embedder.detokenize(token_slice).decode('utf-8', errors='ignore')
            
            chunks.append(chunk_text)
            
            # Advance the window forward, subtracting the token overlap
            start_idx += (chunk_size - overlap)
            
            # Safety break to avoid infinite loops if overlap >= chunk_size
            if chunk_size <= overlap:
                break
                
        return chunks
    

    def generate_file_id(self, cursor) -> str:
        while True:
            file_id = uuid.uuid4().hex[:12]
            cursor.execute("SELECT 1 FROM file_metadata WHERE file_id = ?", (file_id,))
            if cursor.fetchone() is None:
                return file_id
            
    def check_duplicate_file_name(self, cursor: Cursor, file_name: str) -> bool:
        cursor.execute("SELECT 1 FROM file_metadata WHERE file_name = ?", (file_name,))
        return cursor.fetchone() is not None


    def ingest(self, file_name: str, file_json: dict[str, list], embedder: Llama):
        """
        Parses base model's output and populates BOTH the single-row file table
        and the individual section rows simultaneously with perfect cursor isolation.
        """
        data = file_json
        sections = data.get("sections", [])

        logger.info(f"Writing {file_name} to Knowledge {os.path.basename(self.db_path)} - {len(sections)} sections")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)

                main_cursor = conn.cursor()
                main_cursor.execute("PRAGMA foreign_keys = ON;")
                chunk_cursor = conn.cursor() # Dedicated cursor to isolate lastrowid state

                if self.check_duplicate_file_name(main_cursor, file_name):
                    logger.error(f"File '{file_name}' already exists in Knowledge: {os.path.basename(self.db_path)}")
                    raise FileExistsError(f"File '{file_name}' already exists in this Knowledge")

                file_id = self.generate_file_id(main_cursor)
                
                main_cursor.execute(
                    "INSERT INTO file_metadata (file_id, file_name, metadata) VALUES (?, ?, '');",
                    (file_id, file_name,)
                )
                
                file_wide_metadata = []
                
                for index, section in enumerate(sections):
                    section_id = f"{file_id}-{index + 1}"
                    raw_text = section.get("raw_content")
                    
                    file_wide_metadata.append({
                        "section_id": section_id,
                        "summary": section.get("summary"),
                        "keywords": section.get("keywords"),
                        "conditions": section.get("conditions"),
                        "definitions": section.get("definitions")
                    })
                    
                    single_section_content = {"raw_content": raw_text}
                    main_cursor.execute("""
                        INSERT INTO section_metadata (file_id, section_id, content_json)
                        VALUES (?, ?, ?);
                    """, (file_id, section_id, json.dumps(single_section_content)))

                    # Slice the text into chunks
                    text_chunks = self._split_by_tokens(
                        text=raw_text,
                        embedder=embedder,
                        chunk_size=CHUNK_SIZE,
                        overlap=CHUNK_OVERLAP
                    )

                    for chunk_index, chunk_text in enumerate(text_chunks):
                        raw_vector = embedder.embed(chunk_text)
                        binary_vector = np.array(raw_vector, dtype=np.float32).tobytes()
                        
                        # We use chunk_cursor here so lastrowid is never contaminated by main_cursor
                        chunk_cursor.execute("""
                            INSERT INTO chunk_metadata (section_id, chunk_index, chunk_text)
                            VALUES (?, ?, ?);
                        """, (section_id, chunk_index, chunk_text))
                        
                        assigned_chunk_id = chunk_cursor.lastrowid

                        chunk_cursor.execute("""
                            INSERT INTO vec_chunks (chunk_id, embedding)
                            VALUES (?, ?);
                        """, (assigned_chunk_id, binary_vector))
                
                main_cursor.execute("""
                    UPDATE file_metadata 
                    SET metadata = ?
                    WHERE file_id = ?;
                """, (json.dumps(file_wide_metadata), file_id))
                
                conn.commit()

                logger.info(f"Successfully ingested {file_name} (ID: {file_id}) with {len(sections)} sections.")
        except Exception as e:
            logger.error(f"Failed to write {file_name} to Knowledge: {e}")
            raise
            

    
    def delete_file(self, file_id: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT file_name 
                FROM file_metadata 
                WHERE file_id = ?
            """, (file_id,))
            
            row = cursor.fetchone()
            filename = row[0] if row else None
        
        logger.info(f"Deleting {filename} - file_id: {file_id}")

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)

                cursor = conn.cursor()
                cursor.execute("PRAGMA foreign_keys = ON;")

                cursor.execute("""
                    DELETE FROM vec_chunks 
                    WHERE chunk_id IN (
                        SELECT cm.chunk_id 
                        FROM chunk_metadata cm
                        JOIN section_metadata sm ON cm.section_id = sm.section_id
                        WHERE sm.file_id = ?
                    )
                """, (file_id,))

                cursor.execute("DELETE FROM file_metadata WHERE file_id = ?", (file_id,))

                conn.commit()

                logger.info(f"Successfully deleted {filename} - file_id: {file_id}")
        except Exception as e:
            logger.error(f"Failed to delete {filename} - file_id: {file_id}")
            raise




class KnowledgeBase:
    """Manages the Knowledge databases"""
    
    def __init__(self, base_path: str = KNOWLEDGE_BASE_DIR):
        self.base_path = base_path
        self.registry = {}

        if not os.path.exists(self.base_path):
            os.makedirs(self.base_path)

        self.load_existing()


    def create_knowledge(self, name, user_summary: str= ""):
        logger.info(f"Creating Knowledge: {name}")

        try:
            path = os.path.join(self.base_path, f"{name}.db")

            knowledge_instance = Knowledge(db_path= path)
            knowledge_instance.set_summary(user_summary)
            
            self.registry.update(
                {
                    name: {
                        "Knowledge": knowledge_instance,
                        "user_summary": user_summary
                    }
                }
            )
            logger.info(f"Successfully created Knowledge: {name}")
        except Exception as e:
            logger.error(f"Failed to create Knnowledge: {name}")
            raise

    

    def get_knowledge(self, name) -> Knowledge:
        return self.registry.get(name, {}).get("Knowledge")
    
    def get_knowledge_summary(self, name) -> str:
        return self.registry.get(name, {}).get("user_summary")
    

    def load_existing(self):
        logger.info("Loading existing Knowledges from disk")

        try:
            for file_name in os.listdir(self.base_path):
                if file_name.endswith(".db"):
                    name = file_name.replace(".db", "")
                    path = os.path.join(self.base_path, file_name)

                    knowledge_instance = Knowledge(db_path= path)

                    with sqlite3.connect(path) as conn:
                        # If you need vector functions, load the extension per-connection
                        conn.enable_load_extension(True)
                        sqlite_vec.load(conn)
                        conn.enable_load_extension(False)
                        
                        cursor = conn.cursor()
                        cursor.execute("SELECT summary_text FROM knowledge_summary WHERE type = 'user_summary'")
                        row = cursor.fetchone()
                        summary = row[0] if row else "No summary provided"
                    
                    self.registry[name] = {
                        "Knowledge": knowledge_instance,
                        "user_summary": summary
                    }

            logger.info(f"Loaded {len(self.registry)} Knowledges: {list(self.registry.keys())}")
        except Exception as e:
            logger.error("Failed to load Knowledges from disk")
            raise

    
    def delete_knowledge(self, name: str):
        try:
            db_path = os.path.join(self.base_path, f"{name}.db")

            if not os.path.exists(db_path):
                logger.error(f"Knowledge: {name} not found")
                raise FileNotFoundError(f"Knowledge: {name} not found")
            

            os.remove(db_path)
            del self.registry[name]

            logger.info(f"Deleted Knowledge: {name}")
        except Exception as e:
            logger.error(f"Failed to delete Knowledge: {name}: {e}")
            raise
