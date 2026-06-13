# product_embeddings.py

import json
from typing import List, Dict
from sentence_transformers import SentenceTransformer
import asyncio
from db import supabase
import logging

logger = logging.getLogger(__name__)

class VectorStore:
    def __init__(self):
        """Initialize vector store"""
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.has_connection = supabase is not None
        
    def create_from_knowledge_base(self, knowledge_base_file: str = "woo_knowledge_base.json"):
        """Create embeddings for all products/pages and save to Supabase"""
        if not self.has_connection:
            logger.error("❌ No Supabase connection available. Cannot save embeddings.")
            return

        with open(knowledge_base_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        products = data.get('products', [])
        pages = data.get('pages', [])
        
        # --- Product Embeddings ---
        logger.info(f"🧠 Creating and saving embeddings for {len(products)} products to Supabase...")
        batch_size = 50
        records = []
        for i, product in enumerate(products):
            # Create searchable text for product
            text = f"""
{product['name']}
{product.get('short_description', '')}
{product.get('description', '')}
Categories: {', '.join(product.get('categories', []))}
Tags: {', '.join(product.get('tags', []))}
Price: ৳{product['price']}
SKU: {product['sku']}
"""
            embedding = self.model.encode(text, convert_to_tensor=False).tolist()
            records.append({
                "id": product['id'],
                "content": text,
                "metadata": product,
                "embedding": embedding
            })
            
            if len(records) >= batch_size or i == len(products) - 1:
                try:
                    supabase.table("product_embeddings").upsert(records).execute()
                    records = []
                except Exception as e:
                    logger.error(f"  ❌ Failed to upload product batch: {e}")
        logger.info("✅ Product embeddings saved!")

        # --- Page/Policy Embeddings ---
        if pages:
            logger.info(f"🧠 Creating and saving embeddings for {len(pages)} pages to Supabase...")
            page_records = []
            for i, page in enumerate(pages):
                text = f"{page['title']}\n{page['content']}"
                embedding = self.model.encode(text, convert_to_tensor=False).tolist()
                page_records.append({
                    "id": page['id'],
                    "title": page['title'],
                    "content": page['content'],
                    "link": page['link'],
                    "embedding": embedding
                })
                
                if len(page_records) >= batch_size or i == len(pages) - 1:
                    try:
                        supabase.table("page_embeddings").upsert(page_records).execute()
                        page_records = []
                    except Exception as e:
                        logger.error(f"  ❌ Failed to upload page batch: {e}")
            logger.info("✅ Page embeddings saved!")
    
    async def search_products(self, query: str, top_k: int = 5) -> List[Dict]:
        """Search products using semantic similarity via Supabase pgvector"""
        if not self.has_connection:
            return []
            
        def _search():
            query_embedding = self.model.encode(query, convert_to_tensor=False).tolist()
            
            try:
                response = supabase.rpc(
                    "match_products",
                    {
                        "query_embedding": query_embedding,
                        "match_threshold": 0.2,
                        "match_count": top_k
                    }
                ).execute()
                
                results = []
                if response.data:
                    for row in response.data:
                        results.append({
                            "product": row["metadata"],
                            "similarity_score": row["similarity"],
                            "search_text": row["content"][:500]
                        })
                return results
            except Exception as e:
                logger.error(f"❌ Supabase semantic product search error: {e}")
                return []
                
        return await asyncio.to_thread(_search)

    async def search_pages(self, query: str, top_k: int = 3) -> List[Dict]:
        """Search store pages/policies using semantic similarity via Supabase pgvector"""
        if not self.has_connection:
            return []
            
        def _search():
            query_embedding = self.model.encode(query, convert_to_tensor=False).tolist()
            
            try:
                response = supabase.rpc(
                    "match_pages",
                    {
                        "query_embedding": query_embedding,
                        "match_threshold": 0.2,
                        "match_count": top_k
                    }
                ).execute()
                
                results = []
                if response.data:
                    for row in response.data:
                        results.append({
                            "id": row["id"],
                            "title": row["title"],
                            "content": row["content"],
                            "link": row["link"],
                            "similarity_score": row["similarity"]
                        })
                return results
            except Exception as e:
                logger.error(f"❌ Supabase semantic page search error: {e}")
                return []
                
        return await asyncio.to_thread(_search)