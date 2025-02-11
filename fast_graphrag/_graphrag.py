"""This module implements a Graph-based Retrieval-Augmented Generation (GraphRAG) system."""

from dataclasses import dataclass, field
from typing import Any, Dict, Generic, List, Optional, Union

from fast_graphrag._llm import BaseLLMService, format_and_send_prompt
from fast_graphrag._llm._base import BaseEmbeddingService
from fast_graphrag._policies._base import BaseEdgeUpsertPolicy, BaseGraphUpsertPolicy, BaseNodeUpsertPolicy
from fast_graphrag._prompt import PROMPTS
from fast_graphrag._services._chunk_extraction import BaseChunkingService
from fast_graphrag._services._information_extraction import BaseInformationExtractionService
from fast_graphrag._services._state_manager import BaseStateManagerService
from fast_graphrag._storage._base import BaseGraphStorage, BaseIndexedKeyValueStorage, BaseVectorStorage
from fast_graphrag._types import GTChunk, GTEdge, GTEmbedding, GTHash, GTId, GTNode, TContext, TDocument, TQueryResponse
from fast_graphrag._utils import TOKEN_TO_CHAR_RATIO, get_event_loop, logger


@dataclass
class InsertParam:
    pass


@dataclass
class QueryParam:
    with_references: bool = False


@dataclass
class BaseGraphRAG(Generic[GTEmbedding, GTHash, GTChunk, GTNode, GTEdge, GTId]):
    """A class representing a Graph-based Retrieval-Augmented Generation system."""

    working_dir: str = field()
    domain: str = field()
    example_queries: str = field()
    entity_types: List[str] = field()
    n_checkpoints: int = field(default=0)

    llm_service: BaseLLMService = field(init=False, default_factory=lambda: BaseLLMService())
    chunking_service: BaseChunkingService[GTChunk] = field(init=False, default_factory=lambda: BaseChunkingService())
    information_extraction_service: BaseInformationExtractionService[GTChunk, GTNode, GTEdge, GTId] = field(
        init=False,
        default_factory=lambda: BaseInformationExtractionService(
            graph_upsert=BaseGraphUpsertPolicy(
                config=None,
                nodes_upsert_cls=BaseNodeUpsertPolicy,
                edges_upsert_cls=BaseEdgeUpsertPolicy,
            )
        ),
    )
    state_manager: BaseStateManagerService[GTNode, GTEdge, GTHash, GTChunk, GTId, GTEmbedding] = field(
        init=False,
        default_factory=lambda: BaseStateManagerService(
            workspace=None,
            graph_storage=BaseGraphStorage[GTNode, GTEdge, GTId](config=None),
            entity_storage=BaseVectorStorage[GTId, GTEmbedding](config=None),
            chunk_storage=BaseIndexedKeyValueStorage[GTHash, GTChunk](config=None),
            embedding_service=BaseEmbeddingService(),
            node_upsert_policy=BaseNodeUpsertPolicy(config=None),
            edge_upsert_policy=BaseEdgeUpsertPolicy(config=None),
        ),
    )

    def insert(
        self,
        content: Union[str, List[str]],
        metadata: Union[List[Optional[Dict[str, Any]]], Optional[Dict[str, Any]]] = None,
        params: Optional[InsertParam] = None,
    ) -> None:
        return get_event_loop().run_until_complete(self.async_insert(content, metadata, params))

    async def async_insert(
        self,
        content: Union[str, List[str]],
        metadata: Union[List[Optional[Dict[str, Any]]], Optional[Dict[str, Any]]] = None,
        params: Optional[InsertParam] = None,
    ) -> None:
        """Insert a new memory or memories into the graph.

        Args:
            content (str | list[str]): The data to be inserted. Can be a single string or a list of strings.
            metadata (dict, optional): Additional metadata associated with the data. Defaults to None.
            params (InsertParam, optional): Additional parameters for the insertion. Defaults to None.
        """
        if params is None:
            params = InsertParam()

        if isinstance(content, str):
            content = [content]
        if isinstance(metadata, dict):
            metadata = [metadata]

        if metadata is None or isinstance(metadata, dict):
            data = (TDocument(data=c, metadata=metadata or {}) for c in content)
        else:
            data = (TDocument(data=c, metadata=m or {}) for c, m in zip(content, metadata))

        await self.state_manager.insert_start()
        try:
            # Chunk the data
            chunked_documents = await self.chunking_service.extract(data=data)

            # Filter the chunks checking for duplicates
            new_chunks_per_data = await self.state_manager.filter_new_chunks(chunks_per_data=chunked_documents)

            # Extract entities and relationships from the new chunks only
            subgraphs = self.information_extraction_service.extract(
                llm=self.llm_service,
                documents=new_chunks_per_data,
                prompt_kwargs={
                    "domain": self.domain,
                    "example_queries": self.example_queries,
                    "entity_types": ",".join(self.entity_types),
                },
            )
            if len(subgraphs) == 0:
                logger.info("No new entities or relationships extracted from the data.")

            # Update the graph with the new entities, relationships, and chunks
            await self.state_manager.upsert(llm=self.llm_service, subgraphs=subgraphs, documents=new_chunks_per_data)
        except Exception as e:
            logger.error(f"Error during insertion: {e}")
            raise e
        finally:
            await self.state_manager.insert_done()

    def query(self, query: str, params: Optional[QueryParam] = None) -> TQueryResponse[GTNode, GTEdge, GTHash, GTChunk]:
        async def _query() -> TQueryResponse[GTNode, GTEdge, GTHash, GTChunk]:
            try:
                await self.state_manager.query_start()
                answer = await self.async_query(query, params)
                return answer
            except Exception as e:
                logger.error(f"Error during query: {e}")
                raise e
            finally:
                await self.state_manager.query_done()

        return get_event_loop().run_until_complete(_query())

    async def async_query(
        self, query: str, params: Optional[QueryParam] = None
    ) -> TQueryResponse[GTNode, GTEdge, GTHash, GTChunk]:
        """Query the graph with a given input.

        Args:
            query (str): The query string to search for in the graph.
            params (QueryParam, optional): Additional parameters for the query. Defaults to None.

        Returns:
            TQueryResponse: The result of the query (response + context).
        """
        if params is None:
            params = QueryParam()

        # Extract entities from query
        extracted_entities = await self.information_extraction_service.extract_entities_from_query(
            llm=self.llm_service, query=query, prompt_kwargs={}
        )

        # Retrieve relevant state
        relevant_state = await self.state_manager.get_context(query=query, entities=extracted_entities)
        if relevant_state is None:
            return TQueryResponse[GTNode, GTEdge, GTHash, GTChunk](
                response=PROMPTS["fail_response"], context=TContext([], [], [])
            )

        # Ask LLM
        llm_response, _ = await format_and_send_prompt(
            prompt_key="generate_response_query",
            llm=self.llm_service,
            format_kwargs={
                "query": query,
                "context": relevant_state.to_str(
                    {
                        "entities": 4000 * TOKEN_TO_CHAR_RATIO,
                        "relationships": 3000 * TOKEN_TO_CHAR_RATIO,
                        "chunks": 9000 * TOKEN_TO_CHAR_RATIO,
                    }
                ),
            },
            response_model=str,
        )

        return TQueryResponse[GTNode, GTEdge, GTHash, GTChunk](response=llm_response, context=relevant_state)
