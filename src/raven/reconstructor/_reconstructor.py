import logging
from raven.core._knowledge_base import KnowledgeBase
from raven.pipelines._pipeline import RetrievalPipeline


logger = logging.getLogger(__name__)


class Reconstructor(RetrievalPipeline):
    def __init__(self, knowledge_base: KnowledgeBase):
        super().__init__(knowledge_base)


    def reconstruct(self, retrieval_result, tool_name):
        try:
            if not retrieval_result:
                return
            
            section_list = self.get_section_list(retrieval_result, tool_name)

            if not section_list:
                return

            # Group retrieved sections by unique file
            logger.info("Reconstructing files...")
            file_map = {}
            for section in section_list:
                key = (section["knowledge_name"], section["file_name"])
                if key not in file_map:
                    file_map[key] = []
                file_map[key].append(section["section_id"])

            # For each unique file, fetch all sections and mark highlights
            reconstructed = []
            for (knowledge_name, file_name), highlighted_ids in file_map.items():

                all_sections = self.list_sections(
                    knowledge_name=knowledge_name,
                    file_name=file_name
                )
                all_sections = sorted(
                    all_sections,
                    key=lambda s: int(s["section_id"].split("-")[-1])
                )

                sections_with_highlights = [
                    {
                        "section_id": s["section_id"],
                        "highlighted": s["section_id"] in highlighted_ids,
                        "raw_content": s["raw_content"],
                    }
                    for s in all_sections
                ]

                reconstructed.append({
                    "knowledge_name": knowledge_name,
                    "file_name": file_name,
                    "sections": sections_with_highlights
                })

            logger.info(f"Reconstruction complete — {len(reconstructed)} file(s)")
            return reconstructed

        except Exception as e:
            logger.error(f"Reconstruction failed: {e}")
            raise


    def get_section_list(self, retrieval_result, tool_name= None):
        if not retrieval_result:
                return

        if tool_name in ["get_memory", "save_preference", None]:
            return

        if isinstance(retrieval_result, str):
            return

        # For Agreement Based Retrieval only
        if isinstance(retrieval_result, dict):
            if retrieval_result.get("agreement_type", None) == "Strong Agreement":
                section_list = retrieval_result.get("retrieved_content", None)
                return section_list
            else:
                embedded_results = retrieval_result.get("retrieved_content", None).get("embedded_retrieval", None)          # type: ignore
                hierarchical_results = retrieval_result.get("retrieved_content", None).get("hierarchical_retrieval", None)  #type: ignore
                section_list = embedded_results + hierarchical_results
                return section_list

        # For any case other than agreement based retrieval
        section_list = retrieval_result
        return section_list