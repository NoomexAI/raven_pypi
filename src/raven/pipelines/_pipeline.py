from llama_cpp import LlamaGrammar
import re
import os
import sqlite3, sqlite_vec
import numpy as np
import json
import logging
import time
from wtpsplit_lite import SaT

from raven.core.constants import INGESTION_SYSTEM_PROMPT, SUBFILE_SIZE, GRAMMAR_DIR, SENTENCES_PER_CHUNK
from raven.core.constants import HIERARCHICAL_RETRIEVAL_SYSTEM_PROMPT, HIERARCHICAL_RETRIEVAL_SUMMARY_SYSTEM_PROMPT
from raven.core._knowledge_base import KnowledgeBase
from raven.status._status import Status

logger = logging.getLogger(__name__)


# Ingestion pipeline
class IngestionPipeline:
    def __init__(self, knowledge_base: KnowledgeBase, base_model, embedding_model, sbd_model, s: Status|None = None):      # type: ignore
        
        self.base_model = base_model
        self.embedding_model = embedding_model
        self.sbd_model = sbd_model
        self.knowledge_base = knowledge_base
        self.s = s

        self.base_model.reset()
        

    
    def _subdivide_file_by_tokens(self, file_path, subfile_size: int):
        """Splits the file into text chunks of maximum `chunk_size` tokens with 0 overlap."""

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read().replace('"', "'")

            # Use the base model's internal tokenizer
            tokens = self.base_model.tokenize(text.encode("utf-8"))
            if len(tokens) <= subfile_size:
                return [text]
            
            logger.info(f"Large file detected ({len(tokens)} tokens) — subdividing into Sub-Files of {subfile_size} tokens")
            chunks = []
            for i in range(0, len(tokens), subfile_size):
                slice_tokens = tokens[i : i + subfile_size]
                # Decode the slice back into text safely
                chunk_text = self.base_model.detokenize(slice_tokens).decode("utf-8", errors="ignore")
                chunks.append(chunk_text)
            
            logger.info(f"Successfully subdivided File into {len(chunks)} Sub-files")
            return chunks
        except Exception as e:
            logger.error(f"Failed to subdivide file {file_path}: {e}")
            raise



    def _chunk_subfile(self, text: str, sentences_per_chunk: int = 4):
        """
        Splits text into chunks of ~chunk_size tokens each.
        Returns a dict {chunk_id: chunk_text} and the original chunks list.
        """
        if isinstance(self.sbd_model, SaT):
            sentences = self.sbd_model.split(text, stride=128, block_size=256, weighting="hat")
        else:
            logger.error("SBD model not found")
            raise FileNotFoundError("SBD model not found")

        chunks = {}
        chunk_id = 1
        
        for i in range(0, len(sentences), sentences_per_chunk):                                 # type: ignore
            group = sentences[i:i + sentences_per_chunk]                                        # type: ignore
            chunks[chunk_id] = "".join(group)
            chunk_id += 1

        return chunks
    
    
    def _build_chunk_prompt(self, chunks: dict) -> str:
        """Formats chunks dict into the numbered list format for the model."""
        lines = []
        for chunk_id, chunk_text in chunks.items():
            # Escape quotes to avoid JSON conflicts
            safe_text = chunk_text.replace('"', "'")
            lines.append(f'{chunk_id}: "{safe_text}"')
        return "\n".join(lines)
    
    
    def _reconstruct_sections(self, chunks: dict, section_boundaries: list) -> list:
        sections = []
        prev_end = 0
        max_chunk_id = max(chunks.keys())

        for idx, section in enumerate(section_boundaries):
            end_chunk_id = min(int(section["end_chunk_id"]), max_chunk_id)

            # Last section always takes everything remaining
            if idx == len(section_boundaries) - 1:
                end_chunk_id = max_chunk_id

            raw_content = "".join(
                chunks[i] for i in range(prev_end + 1, end_chunk_id + 1)
                if i in chunks
            )

            sections.append({
                "summary": section.get("summary", ""),
                "keywords": section.get("keywords", []),
                "conditions": section.get("conditions", []),
                "definitions": section.get("definitions", []),
                "raw_content": raw_content
            })

            prev_end = end_chunk_id

        return sections


    def strip_thinking(self, content: str):
        return re.sub(r'<\|channel>.*?<channel\|>', '', content, flags=re.DOTALL).strip()


    def ingest(self, knowledge_name, file_path: str, force_stabilize: bool= False):

        if not os.path.exists(file_path):
            logger.error(f"File not found: {file_path}")
            raise FileNotFoundError(f"File: {file_path} does not exist")
        
        self.grammar = None
        
        try:
            if force_stabilize:
                schema = json.loads(GRAMMAR_DIR.joinpath("ingestion_schema.json").read_text())

                self.grammar = LlamaGrammar.from_json_schema(json.dumps(schema))

                if self.s is not None:
                    self.s.status = self.s.registry.INGESTION_GRAMMAR_ENFORCED                                         #type: ignore
                logger.info("Ingestion Pipeline initialized with grammar stabilization")
            else:
                logger.info("Ingestion Pipeline initialized without grammar stabilization")
        except Exception as e:
            logger.error(f"Failed to initialized ingestion grammar: {e}")
            raise RuntimeError(f"Failed to initialized ingestion grammar: {e}")
        

        file_name = os.path.basename(file_path)
        logger.info(f"Ingestion started: {file_name}")

        logger.info("Reading files...")

        if self.s is not None:
            self.s.status = self.s.registry.SUBDIVIDING_FILE                                                         #type: ignore
        file_subdivisions = self._subdivide_file_by_tokens(file_path, SUBFILE_SIZE)
        if self.s is not None:
            self.s.status = self.s.registry.FILE_SUBDIVIDED                                                           #type: ignore

        logger.info("Running inference")

        max_retries = 3

        file_subdivisions_dicts = []

        if self.s is not None:
            self.s.status = self.s.registry.INGESTION_INFERENCE_RUNNING                                               #type: ignore
        
        for i, subfile_text in enumerate(file_subdivisions):
            logger.info(f"Running inference on subfile: {i+1}/{len(file_subdivisions)}")
            chunks = self._chunk_subfile(subfile_text, sentences_per_chunk=SENTENCES_PER_CHUNK)
            chunk_prompt = self._build_chunk_prompt(chunks)

            for attempt in range(max_retries):
                try:
                    chat_payload = [
                        {"role": "system", "content": INGESTION_SYSTEM_PROMPT},
                        {"role": "user", "content": chunk_prompt}
                    ]

                    response = self.base_model.create_chat_completion(
                        messages= chat_payload,                                                 #type: ignore
                        temperature= 0.6,
                        top_p= 0.95,
                        top_k= 64,
                        max_tokens= 8192,
                        stream= False,
                        grammar= self.grammar
                    )

                    base_model_output = response["choices"][0]["message"]["content"]            #type: ignore
                    base_model_output = self.strip_thinking(base_model_output)
                    base_model_output_json = json.loads(base_model_output.strip())

                    reconstructed = self._reconstruct_sections(
                        chunks, base_model_output_json["sections"]
                    )
                    file_subdivisions_dicts.extend(reconstructed)

                    logger.info(f"{i+1}/{len(file_subdivisions)} Sub-Files processed successfully")
                    break

                except json.JSONDecodeError:
                    if attempt < max_retries - 1:
                        logger.warning(f"Sub-File {i+1}, attempt {attempt+1} failed — retrying")
                    else:
                        logger.error(f"Sub-File {i+1} skipped after {max_retries} failed attempts")

        if self.s is not None:
            self.s.status = self.s.registry.INGESTION_INFERENCE_COMPLETE                                          #type: ignore
        
        file_json = {
            "sections": file_subdivisions_dicts
        }

        logger.info("Inference Complete")

        self.knowledge = self.knowledge_base.get_knowledge(knowledge_name)

        if self.s is not None:
            self.s.status = self.s.registry.INGESTING                                                             #type: ignore
        self.knowledge.ingest(
            file_name,
            file_json,                                                 
            embedder= self.embedding_model
        )
        if self.s is not None:
            self.s.status = self.s.registry.INGESTION_COMPLETE                                                    #type: ignore
        logger.info("Ingestion Complete")




# Parent Class - Retrieval Pipeline
class RetrievalPipeline:
    def __init__(self, knowledge_base):                                 #type: ignore
        self.knowledge_base = knowledge_base
        self.knowledge_base_path = knowledge_base.base_path

    def search_vectors(self, knowledge_name, query_vector, k2=1):
        try:
            # 1. Convert numpy array to the bytes format sqlite-vec expects
            binary_query = np.array(query_vector, dtype=np.float32).tobytes()

            db_path = os.path.join(self.knowledge_base_path, f"{knowledge_name}.db")

            if not os.path.exists(db_path):
                logger.error(f"Knowledge: {knowledge_name} does not exist")
                raise FileNotFoundError(f"Knowledge: {knowledge_name} does not exist")

            with sqlite3.connect(db_path) as conn:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
                
                cursor = conn.cursor()
                
                # 2. Join with chunk_metadata to return text directly
                # 3. Use the distance function specifically provided by sqlite-vec
                
                cursor.execute("""
                    SELECT 
                        c.chunk_text, 
                        c.section_id, 
                        v.distance
                    FROM vec_chunks v
                    JOIN chunk_metadata c ON v.chunk_id = c.chunk_id
                    WHERE v.embedding MATCH ?
                    AND k = ?
                    ORDER BY v.distance
                    LIMIT ?
                """, (binary_query, k2, k2)
                )

                results = [
                    {"knowledge_name": knowledge_name, "text": row[0], "section_id": row[1], "distance": row[2]}
                    for row in cursor.fetchall()
                ]

            logger.info(f"Vector search in {knowledge_name} returned {len(results)} results")
            return results
        except Exception as e:
            logger.error(f"Vector search failed in {knowledge_name}: {e}")
            raise

    

    def get_section_content(self, knowledge_name, section_id):
        try:
            db_path = os.path.join(self.knowledge_base_path, f"{knowledge_name}.db")

            if not os.path.exists(db_path):
                logger.error(f"Knowledge: {knowledge_name} does not exist")
                raise FileNotFoundError(f"Knowledge: {knowledge_name} does not exist")
            
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                # Fetch the raw content from the section_metadata table
                cursor.execute("""
                    SELECT content_json 
                    FROM section_metadata 
                    WHERE section_id = ?
                """, (section_id,))
                
                row = cursor.fetchone()
                
                if not row:
                    logger.warning(f"Section: {section_id} not found in Knowledge: {knowledge_name}")
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to fetch section: {section_id} from Knowledge: {knowledge_name}: {e}")
            raise


    def get_file_name(self, knowledge_name: str, file_id: str):
        try:
            db_path = os.path.join(self.knowledge_base_path, f"{knowledge_name}.db")

            if not os.path.exists(db_path):
                logger.error(f"Knowledge: {knowledge_name} does not exist")
                raise FileNotFoundError(f"Knowledge: {knowledge_name} does not exist")

            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT file_name 
                    FROM file_metadata 
                    WHERE file_id = ?
                """, (file_id,))
                
                row = cursor.fetchone()

                if not row:
                    logger.warning(f"File not found: file_id {file_id} in Knowledge: {knowledge_name}")
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to fetch file name for file_id: {file_id} in Knowledge: {knowledge_name}: {e}")
            raise
    

    def get_metadata(self, knowledge_name: str):
        try:
            db_path = os.path.join(self.knowledge_base_path, f"{knowledge_name}.db")
            
            if not os.path.exists(db_path):
                logger.error(f"Knowledge: {knowledge_name} does not exist")
                raise FileNotFoundError(f"Knowledge: {knowledge_name} does not exist")
                

            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT metadata FROM file_metadata")
                file_metadata = [row[0] for row in cursor.fetchall()]

                result = {
                    "knowledge_name": knowledge_name,
                    "file_metadata": file_metadata
                }

            logger.info(f"Fetched metadata from Knowledge: {knowledge_name} — {len(file_metadata)} files")
            return f"<startk>{result}<endk>"
        except Exception as e:
            logger.error(f"Failed to fetch metadata from Knowledge: {knowledge_name}: {e}")
            raise

    
    def get_metadata_by_section(self, knowledge_name: str, section_id: str):
        try:
            db_path = os.path.join(self.knowledge_base_path, f"{knowledge_name}.db")

            if not os.path.exists(db_path):
                logger.error(f"Knowledge: {knowledge_name} does not exist")
                raise FileNotFoundError(f"Knowledge: {knowledge_name} does not exist")

            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT fm.metadata
                    FROM file_metadata fm
                    JOIN section_metadata sm ON fm.file_id = sm.file_id
                    WHERE sm.section_id = ?
                """, (section_id,))
                row = cursor.fetchone()

                if not row:
                    return None

                # file_metadata stores a list of all sections' metadata
                sections = json.loads(row[0])
                for section in sections:
                    if section["section_id"] == section_id:
                        return {
                            "summary": section.get("summary", ""),
                            "keywords": section.get("keywords", []),
                            "conditions": section.get("conditions", []),
                            "definitions": section.get("definitions", [])
                        }
                return None

        except Exception as e:
            logger.error(f"Failed to get section metadata: {section_id}: {e}")
            raise



    def get_knowledge_summary(self, knowledge_name: str):
        return self.knowledge_base.get_knowledge_summary(knowledge_name)
    

    # Special toolset

    def list_knowledges(self):
        try:
            knowledge_list = [
                {
                    "knowledge_name": name,
                    "user_summary": self.get_knowledge_summary(name)
                }
                for name in self.knowledge_base.registry.keys()
            ]
            logger.info(f"Listed {len(knowledge_list)} Knowledges")
            return knowledge_list
        except Exception as e:
            logger.error(f"Failed to list Knowledges: {e}")
            raise

    def list_files(self, knowledge_name: str):
        try:
            db_path = os.path.join(self.knowledge_base_path, f"{knowledge_name}.db")

            if not os.path.exists(db_path):
                logger.error(f"Knowledge: {knowledge_name} does not exist")
                raise FileNotFoundError(f"Knowledge: {knowledge_name} does not exist")

            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT file_id, file_name FROM file_metadata")
                files = [{"file_id": row[0], "file_name": row[1]} for row in cursor.fetchall()]

            logger.info(f"Listed {len(files)} files in Knowledge: {knowledge_name}")
            return files
        
        except Exception as e:
            logger.error(f"Failed to list files in Knowledge: {knowledge_name}: {e}")
            raise

    
    def list_sections(self, knowledge_name: str, file_name: str):
        try:
            db_path = os.path.join(self.knowledge_base_path, f"{knowledge_name}.db")

            if not os.path.exists(db_path):
                logger.error(f"Knowledge: {knowledge_name} does not exist")
                raise FileNotFoundError(f"Knowledge '{knowledge_name}' not found")

            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT sm.section_id, sm.content_json, fm.metadata
                    FROM section_metadata sm
                    JOIN file_metadata fm ON sm.file_id = fm.file_id
                    WHERE fm.file_name = ?
                    ORDER BY sm.section_id
                """, (file_name,))
                rows = cursor.fetchall()

            if not rows:
                logger.warning(f"No sections found for file: {file_name} in Knowledge: {knowledge_name}")
                return []

            file_metadata = json.loads(rows[0][2])
            sections = []
            for row in rows:
                section_id = row[0]
                raw_content = json.loads(row[1]).get("raw_content")
                section_meta = next(
                    (s for s in file_metadata if s.get("section_id") == section_id),
                    {}
                )
                sections.append({
                    "section_id": section_id,
                    "summary": section_meta.get("summary"),
                    "keywords": section_meta.get("keywords"),
                    "conditions": section_meta.get("conditions"),
                    "definitions": section_meta.get("definitions"),
                    "raw_content": raw_content
                })

            logger.info(f"Listed {len(sections)} sections for file: {file_name} in Knowledge: {knowledge_name}")
            return sections
        except Exception as e:
            logger.error(f"Failed to list sections for file: {file_name} in Knowledge: {knowledge_name}: {e}")
            raise



# Retrieval Pipeline - Embedded Retrieval
class EmbeddedRetrievalPipeline(RetrievalPipeline):
    def __init__(self, knowledge_base, embedding_model):
        super().__init__(knowledge_base)

        self.embedding_model = embedding_model

        self.knowledge_base = knowledge_base
        self.knowledge_base_path = knowledge_base.base_path
        
    
    def retrieve_local_context(self, knowledge_name: str, user_query: str, k1=3, k2=3):
        start_time = time.time()

        logger.info(f"Performing Local Embedded Retrieval in Knowledge: {knowledge_name} — query: {user_query[:50]}...")
        try:
            query_vec = self.embedding_model.embed(user_query)
            
            results = self.search_vectors(knowledge_name, query_vec, k2)
            results.sort(key=lambda x: x["distance"])
            results = results[:k1] if len(results) > k1 else results

            final_context = []
            for result in results:
                section_id = result["section_id"]
                file_id, _ = section_id.split("-")

                file_name = self.get_file_name(knowledge_name, file_id)

                raw_content = self.get_section_content(
                    knowledge_name=knowledge_name,
                    section_id= section_id
                )
                final_context.append({
                    "knowledge_name": knowledge_name,
                    "file_name": file_name,
                    "section_id": result["section_id"],
                    "raw_content": raw_content
                })
            
            logger.info(f"Local Embedded Retrieval in Knowledge: {knowledge_name} complete - retrieved {len(final_context)} sections - time: {time.time() - start_time:.2f}s")
            return final_context
        except Exception as e:
            logger.error(f"Local Embedded Local retrieval failed in Knowledge: {knowledge_name}: {e}")
            raise


    def retrieve_global_context(self, user_query: str, k1= 3, k2= 3):
        start_time = time.time()

        logger.info(f"Performing Global Embedded Retrieval — query: {user_query[:50]}...")
        try:
            query_vec = self.embedding_model.embed(user_query)
            results = []

            for name, entry in self.knowledge_base.registry.items():
                local_results = self.search_vectors(name, query_vec, k2)
                results.extend(local_results)

            results.sort(key= lambda x: x["distance"])
            results = results[:k1] if len(results) > k1 else results

            final_context = []
            for result in results:
                section_id = result["section_id"]
                file_id, _ = section_id.split("-")

                file_name = self.get_file_name(result["knowledge_name"], file_id)

                raw_content = self.get_section_content(
                    knowledge_name= result["knowledge_name"],
                    section_id= section_id
                )
                final_context.append({
                    "knowledge_name": result["knowledge_name"],
                    "file_name": file_name,
                    "section_id": result["section_id"],
                    "raw_content": raw_content
                })

            logger.info(f"Global Embedded Retrieval complete - retrieved {len(final_context)} sections - time: {time.time() - start_time:.2f}s")
            return final_context
        except Exception as e:
            logger.error(f"Embedded global retrieval failed: {e}")
            raise




# Retrieval Pipeline - Hierarchical Retrieval
class HierarchicalRetrievalPipeline(RetrievalPipeline):
    def __init__(self, knowledge_base, base_model):
        super().__init__(knowledge_base)

        self.knowledge_base = knowledge_base
        self.knowledge_base_path = knowledge_base.base_path

        self.base_model = base_model

    
    def strip_thinking(self, content):
        return re.sub(r'<\|channel>.*?<channel\|>', '', content, flags=re.DOTALL).strip()
    

    def get_section_id_list(self, user_query, metadata, top_k, grammar):
        logger.info(f"Fetching {top_k} section ids from metadata")

        try:
            messages = [
                {
                    "role": "system",
                    "content": HIERARCHICAL_RETRIEVAL_SYSTEM_PROMPT(top_k= top_k)
                },
                {
                    "role": "user",
                    "content": f"user_query: {user_query}, metadata: {metadata}"
                }
            ]

            response = self.base_model.create_chat_completion(
                messages=messages,                                                      #type: ignore
                temperature= 0.6,
                top_k= 64,
                top_p= 0.95,
                stream= False,
                grammar = grammar
            )

            output = response["choices"][0]["message"]["content"].strip()
            output = self.strip_thinking(output)
            output = json.loads(output)
            section_id_list = output.get("section_id_list")

            logger.info(f"Selected section ids: {section_id_list}")
            return section_id_list
        except json.JSONDecodeError as e:
            logger.error(f"Malformed json detected: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to fetch section ids from metadata: {e}")
            raise
    

    def retrieve_local_context(self, knowledge_name: str, user_query: str, top_k_section: int= 3, force_stabilize = True):
        start_time = time.time()

        logger.info(f"Performing Local Hierarchical Retrieval in Knowledge {knowledge_name}")
        grammar = None

        try:
            if force_stabilize:
                schema = json.loads(GRAMMAR_DIR.joinpath("section_id_schema.json").read_text())

                grammar = LlamaGrammar.from_json_schema(json.dumps(schema))

                logger.info("Performing retrieval with grammar stabilization")
            else:
                logger.info("Performing retrieval without grammar stabilization")
        except Exception as e:
            logger.error(f"Failed to initiate grammar stabilization: {e}")
            raise

        try:
            metadata = self.get_metadata(knowledge_name)

            section_id_list = self.get_section_id_list(
                user_query=user_query,
                metadata=metadata,
                top_k= top_k_section,
                grammar=grammar,
            )

            section_list = []
            for section_id, name in section_id_list:
                file_id, _ = section_id.split("-")
                file_name = self.get_file_name(name, file_id)

                section_content = self.get_section_content(
                    knowledge_name= name,
                    section_id= section_id
                )
                section_list.append(
                    {
                        "knowledge_name": name,
                        "file_name": file_name,
                        "section_id": section_id,
                        "raw_content": section_content
                    }
                )

            logger.info(f"Local Hierarchical Retrieval complete — {len(section_list)} sections retrieved - time: {time.time() - start_time:.2f}s")
            return section_list
        except Exception as e:
            logger.error(f"Local Hierarchical Retrieval failed in Knowledge: {knowledge_name}: {e}")
            raise
    

    def retrieve_global_context(self, user_query: str, top_k_knowledge: int= 2, top_k_section: int= 3, force_stabilize: bool= True, full_retrieval: bool= False):
        start_time = time.time()

        logger.info(f"Performing Global Hierarchical Retrieval — full_retrieval: {full_retrieval}")

        section_id_grammar = None
        knowledge_name_grammar = None

        try:
            if force_stabilize:
                section_id_schema = json.loads(GRAMMAR_DIR.joinpath("section_id_schema.json").read_text())
                knowledge_name_schema = json.loads(GRAMMAR_DIR.joinpath("knowledge_name_schema.json").read_text())

                section_id_grammar = LlamaGrammar.from_json_schema(json.dumps(section_id_schema))
                knowledge_name_grammar = LlamaGrammar.from_json_schema(json.dumps(knowledge_name_schema))
                logger.info("Performing retrieval with grammar stabilization")
            else:
                logger.info("Performing retrieval without grammar stabilization")
        except Exception as e:
            logger.error(f"Failed to initiate grammar stabilization: {e}")
            raise

        
        try:
            if full_retrieval:
                logger.info("Full retrieval mode — Searching over all Knowledge metadata")
                global_metadata = []
                for knowledge_name in self.knowledge_base.registry.keys():
                    metadata = self.get_metadata(knowledge_name)
                    global_metadata.append(metadata)

                global_section_id_list = self.get_section_id_list(
                    user_query= user_query,
                    metadata= global_metadata,
                    top_k= top_k_section,
                    grammar= section_id_grammar
                )

                global_sections_list = []

                for section_id, name in global_section_id_list:

                    file_id, _ = section_id.split("-")
                    file_name = self.get_file_name(name, file_id)

                    section_content = self.get_section_content(
                        knowledge_name= name,
                        section_id= section_id
                    )
                    global_sections_list.append(
                        {
                            "knowledge_name": name,
                            "file_name": file_name,
                            "section_id": section_id,
                            "raw_content": section_content
                        }
                    )

                logger.info(f"Full Global Hierarchical Retrieval complete — {len(global_sections_list)} sections retrieved - time: {time.time() - start_time:.2f}s")
                return global_sections_list


            logger.info(f"Smart global retrieval mode — selecting top {top_k_knowledge} Knowledges")

            knowledge_summary_list = []
            for knowledge_name in self.knowledge_base.registry.keys():
                knowledge_summary = self.get_knowledge_summary(knowledge_name)
                knowledge_summary_list.append({"knowledge_name": knowledge_name, "knowledge_summary": knowledge_summary})

            messages = [
                {
                    "role": "system",
                    "content": HIERARCHICAL_RETRIEVAL_SUMMARY_SYSTEM_PROMPT(top_k= top_k_knowledge)
                },
                {
                    "role": "user",
                    "content": f"user_query: {user_query}, summary_metadata: {knowledge_summary_list}"
                }
            ]

            response = self.base_model.create_chat_completion(
                messages=messages,                                                      #type: ignore
                temperature= 0.6,
                top_k= 64,
                top_p= 0.95,
                stream= False,
                grammar= knowledge_name_grammar
            )
            output = response["choices"][0]["message"]["content"].strip()
            output = self.strip_thinking(output)
            output = json.loads(output)
            knowledge_name_list = output.get("knowledge_name_list")

            logger.info(f"Selected Knowledges: {knowledge_name_list}")


            global_metadata = []
            for knowledge_name in knowledge_name_list:
                metadata = self.get_metadata(knowledge_name)
                global_metadata.append(metadata)


            global_section_id_list = self.get_section_id_list(
                user_query= user_query,
                metadata= global_metadata,
                top_k= top_k_section,
                grammar= section_id_grammar
            )

            global_sections_list = []
            for section_id, name in global_section_id_list:

                file_id, _ = section_id.split("-")
                file_name = self.get_file_name(name, file_id)


                section_content = self.get_section_content(
                    knowledge_name= name,
                    section_id= section_id
                )
                global_sections_list.append(
                    {
                        "knowledge_name": name,
                        "file_name": file_name,
                        "section_id": section_id,
                        "raw_content": section_content
                    }
                )

            logger.info(f"Global Hierarchical Retrieval complete — {len(global_sections_list)} sections retrieved - time: {time.time() - start_time}s")
            return global_sections_list
        
        except json.JSONDecodeError as e:
            logger.error(f"Global Hierarchical Retrieval produced malformed JSON: {e}")
            raise
        except Exception as e:
            logger.error(f"Global Hierarchical Retrieval failed: {e}")
            raise
    

    def retrieve_by_knowledge(self, user_query: str, knowledge_name_list: list, top_k_section: int= 3, force_stabilize: bool= True):
        start_time = time.time()

        logger.info(f"Performing Hierarchical Retrieval withing the Knowledges: {knowledge_name_list}")
        section_id_grammar = None

        try:
            if force_stabilize:
                section_id_schema = json.loads(GRAMMAR_DIR.joinpath("section_id_schema.json").read_text())

                section_id_grammar = LlamaGrammar.from_json_schema(json.dumps(section_id_schema))
                logger.info("Performing retrieval with grammar stabilization")
            else:
                logger.info("Performing retrieval without grammar stabilization")
        except Exception as e:
            logger.error("Failed to initiate grammar stabilization")
            raise
        

        try:
            focused_metadata = []
            for knowledge_name in knowledge_name_list:
                metadata = self.get_metadata(knowledge_name)
                focused_metadata.append(metadata)


            focused_section_id_list = self.get_section_id_list(
                user_query= user_query,
                metadata= focused_metadata,
                top_k= top_k_section,
                grammar= section_id_grammar
            )

            focused_section_list = []
            for section_id, name in focused_section_id_list:
                file_id, _ = section_id.split("-")
                file_name = self.get_file_name(name, file_id)

                section_content = self.get_section_content(
                    knowledge_name= name,
                    section_id= section_id
                )
                focused_section_list.append(
                    {
                        "knowledge_name": name,
                        "file_name": file_name,
                        "section_id": section_id,
                        "raw_content": section_content
                    }
                )

            logger.info(f"Retrieval by Knowledge Complete in Knowledges: {knowledge_name_list} - {len(focused_section_list)} sections retrieved - time: {time.time() - start_time:.2f}s")
            return focused_section_list
        
        except json.JSONDecodeError as e:
            logger.error(f"Hierarchical Retrieval by Knowledge produced malformed JSON: {e}")
            raise
        except Exception as e:
            logger.error(f"Hierarchical Retrieval by Knowledge failed: {e}")
            raise


    def retrieve_by_file(self, user_query: str, knowledge_name: str, file_name_list: list, top_k: int= 5, force_stabilize: bool= True):
        start_time = time.time()

        db_path = os.path.join(self.knowledge_base_path, f"{knowledge_name}.db")

        if not os.path.exists(db_path):
            logger.error(f"Knowledge: {knowledge_name} does not exist")
            raise FileNotFoundError(f"Knowledge: {knowledge_name} does not exist")

        section_id_grammar = None

        try:
            if force_stabilize:
                section_id_schema = json.loads(GRAMMAR_DIR.joinpath("section_id_schema.json").read_text())

                section_id_grammar = LlamaGrammar.from_json_schema(json.dumps(section_id_schema))
                logger.info("Performing retrieval with grammar stabilization")
            else:
                logger.info("Performing retrieval without grammar stabilization")
        except Exception as e:
            logger.error("Failed to initiate grammar stabilization")
            raise
        

        try:
            metadata = []
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()

                for file_name in file_name_list:
                    cursor.execute("""
                        SELECT metadata 
                        FROM file_metadata 
                        WHERE file_name = ?
                    """, (file_name,))
                
                    row = cursor.fetchone()
                    file_metadata = row[0] if row else None

                    result=  {
                        "knowledge_name": knowledge_name,
                        "file_metadata": file_metadata
                    }

                    metadata.append(result)
                
                logger.info(f"Retrieved {len(metadata)} from files: {file_name_list}")
                metadata = f"<startk>{metadata}<endk>"


            section_id_list = self.get_section_id_list(
                user_query= user_query,
                metadata= metadata,
                top_k= top_k,
                grammar= section_id_grammar
            )

            section_list = []
            for section_id, name in section_id_list:
                db_path = os.path.join(self.knowledge_base_path, f"{name}.db")

                file_id, _ = section_id.split("-")
                file_name = self.get_file_name(name, file_id)

                section_content = self.get_section_content(
                    knowledge_name= name,
                    section_id= section_id
                )

                section_list.append(
                    {
                        "knowledge_name": name,
                        "file_name": file_name,
                        "section_id": section_id,
                        "raw_content": section_content
                    }
                )

            logger.info(f"Hierarchical Retrieval by File complete - retrieved {len(section_list)} sections - time: {time.time() - start_time:.2f}")
            return section_list
        
        except Exception as e:
            logger.error(f"Hierarchical Retrieval by File failed: {e}")
            raise



# Retrieval PipeLine - Agreement-based Retrieval
class AgreementBasedRetrievalPipeline(RetrievalPipeline):
    def __init__(self, knowledge_base, embedded_retrieval_pipeline: EmbeddedRetrievalPipeline, hierarchical_retrieval_pipeline: HierarchicalRetrievalPipeline):
        super().__init__(knowledge_base)

        self.embedded_retrieval_pipeline = embedded_retrieval_pipeline
        self.hierarchical_retrieval_pipeline = hierarchical_retrieval_pipeline
    

    def check_agreement(self, embedded_results, hierarchical_results):
        if embedded_results == hierarchical_results:
            return "Strong Agreement"
        
        for e_result, h_result in zip(embedded_results, hierarchical_results):
            e_section_id = e_result.get("section_id", None)
            h_section_id = h_result.get("section_id", None)

            e_file_id, _ = e_section_id.split("-")
            h_file_id, _ = h_section_id.split("-")

            if e_file_id != h_file_id:
                return "Disagreement"


        return "Weak Agreement"


    def retrieve_local_context(self, knowledge_name: str, user_query: str, top_k = 3):
        start_time = time.time()

        logger.info(f"Performing Local Agreement-based Retrieval in Knowledge: {knowledge_name} - user_query: {user_query[:50]}...")

        embedded_results = self.embedded_retrieval_pipeline.retrieve_local_context(
            knowledge_name= knowledge_name,
            user_query= user_query,
            k1= 4,
            k2= 4
        )

        hierarchical_results = self.hierarchical_retrieval_pipeline.retrieve_local_context(
            knowledge_name= knowledge_name,
            user_query= user_query,
            top_k_section= 4,
        )

        logger.info(f"Local Agreement-based Retrieval on Knowledge: {knowledge_name} complete - retrieved {len(embedded_results)} embedded_results and {len(hierarchical_results)} hierarchical results - time: {time.time() - start_time:.2f}s")
        agreement = self.check_agreement(embedded_results, hierarchical_results)

        logger.info(f"Agreement type: {agreement}")

        if agreement == "Strong Agreement":
            return {
                "agreement_type": "Strong Agreement",
                "retrieved_content": embedded_results[:top_k]
            }

        elif agreement == "Weak Agreement":
            return {
                "agreement_type": "Weak Agreement",
                "retrieved_content": {
                    "embedded_retrieval": embedded_results[:top_k],
                    "hierarchical_retrieval": hierarchical_results[:top_k]
                }
            }

        else:
            return {
                "agreement_type": "Disagreement",
                "retrieved_content": {
                    "embedded_retrieval": embedded_results[:top_k],
                    "hierarchical_retrieval": hierarchical_results[:top_k]
                }
            }


    def retrieve_global_context(self, user_query: str, full_retrieval: bool= False, top_k= 3):
        start_time = time.time()

        logger.info(f"Performing Global Agreement-based retrieval - user_query: {user_query[:50]}...")

        embedded_results = self.embedded_retrieval_pipeline.retrieve_global_context(
            user_query= user_query,
            k1= 4,
            k2= 4
        )

        hierarchical_results = self.hierarchical_retrieval_pipeline.retrieve_global_context(
            user_query= user_query,
            top_k_knowledge= 2,
            top_k_section= 4,
            full_retrieval= full_retrieval
        )

        agreement = self.check_agreement(embedded_results, hierarchical_results)

        logger.info(f"Global Agreement-based Retrieval complete - retrieved {len(embedded_results)} embedded_results and {len(hierarchical_results)} hierarchical results - time: {time.time() - start_time:.2f}s")
        logger.info(f"Agreement type: {agreement}")

        if agreement == "Strong Agreement":
            return {
                "agreement_type": "Strong Agreement",
                "retrieved_content": embedded_results[:top_k]
            }

        elif agreement == "Weak Agreement":
            return {
                "agreement_type": "Weak Agreement",
                "retrieved_content": {
                    "embedded_retrieval": embedded_results[:top_k],
                    "hierarchical_retrieval": hierarchical_results[:top_k]
                }
            }

        else:
            return {
                "agreement_type": "Disagreement",
                "retrieved_content": {
                    "embedded_retrieval": embedded_results[:top_k],
                    "hierarchical_retrieval": hierarchical_results[:top_k]
                }
            }
        


# Retrieval Pipeline - Vector Conditioned Retrieval
class VectorConditionedRetrievalPipeline(RetrievalPipeline):
    def __init__(self, knowledge_base, embedded_retrieval_pipeline: EmbeddedRetrievalPipeline, hierarchical_retrieval_pipeline: HierarchicalRetrievalPipeline):
        super().__init__(knowledge_base)

        self.embedded_retrieval_pipeline = embedded_retrieval_pipeline
        self.hierarchical_retrieval_pipeline = hierarchical_retrieval_pipeline

    def retrieve_local_context(self, user_query: str, knowledge_name: str, top_k: int= 5):
        start_time = time.time()

        logger.info(f"Performing Local Vector-conditioned Retrieval in Knowledge: {knowledge_name}")

        embedded_results = self.embedded_retrieval_pipeline.retrieve_local_context(
            user_query= user_query,
            knowledge_name= knowledge_name,
            k1= 5,
            k2= 5
        )

        section_id_list = [item["section_id"] for item in embedded_results]

        logger.info(f"Retrieved section ids by Embedded Retrieval: {section_id_list}")

        file_name_list = []
        for section_id in section_id_list:
            file_id, section_index = section_id.split("-")
            file_name = self.get_file_name(knowledge_name, file_id)
            file_name_list.append(file_name)
        
        file_name_list = list(set(file_name_list))

        logger.info(f"Retrieved file names: {file_name_list}")

        logger.info("Performing Hierarchical Retrieval on retrieved files")
        hierarchical_results = self.hierarchical_retrieval_pipeline.retrieve_by_file(
            user_query= user_query,
            knowledge_name= knowledge_name,
            file_name_list= file_name_list,
            top_k= top_k
        )

        logger.info(f"Local Vector-conditioned Retrieval in Knowledge: {knowledge_name} complete - retrieved {len(hierarchical_results)} sections - time: {time.time() - start_time:.2f}s")
        return hierarchical_results


    def retrieve_global_context(self, user_query: str):
        start_time = time.time()

        logger.info(f"Performing Global Vector-conditioned Retrieval")

        embedded_results = self.embedded_retrieval_pipeline.retrieve_global_context(
            user_query= user_query,
            k1= 10,
            k2= 10
        )

        knowledge_name_list = list(set([item["knowledge_name"] for item in embedded_results]))

        logger.info(f"Retrieved Knowledges by Embedded Retrieval: {knowledge_name_list}")
        
        logger.info("Performing Hierarchical Retrieval on retrieved Knowledges")

        hierarchical_results = self.hierarchical_retrieval_pipeline.retrieve_by_knowledge(
            knowledge_name_list= knowledge_name_list,
            user_query= user_query,
            top_k_section= 5,
        )

        logger.info(f"Global Vector-conditioned Retrieval complete - retrieved {len(hierarchical_results)} sections - time: {time.time() - start_time:.2f}s")
        return hierarchical_results





# Memory Pipeline
class MemoryPipeline:
    def __init__(self, embedding_model):
        self.embedding_model = embedding_model


    def get_memory(self, query: str, conversation, top_k: int = 3):
        try:
            logger.info(f"Memory retrieval — query: {query[:50]}...")

            binary_query = np.array(
                self.embedding_model.embed(query),
                dtype=np.float32
            ).tobytes()

            with sqlite3.connect(conversation.messages_db_path) as conn:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)

                cursor = conn.cursor()
                cursor.execute("""
                    SELECT m.role, m.content, v.distance
                    FROM vec_messages v
                    JOIN messages m ON v.message_id = m.message_id
                    WHERE v.embedding MATCH ?
                    AND k = ?
                    ORDER BY v.distance
                """, (binary_query, top_k))
                
                results = [
                    {"role": row[0], "content": row[1], "distance": row[2]}
                    for row in cursor.fetchall()
                ]

            logger.info(f"Memory retrieval returned {len(results)} results")
            return results

        except Exception as e:
            logger.error(f"Memory retrieval failed: {e}")
            raise


    def get_preference(self, conversation):
        return conversation.get_preference()