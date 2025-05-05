import os
import gc
import tempfile
import uuid
import pdfplumber
from PyPDF2 import PdfReader, PdfWriter
import spacy
from PIL import Image
import matplotlib.pyplot as plt
import threading

from llama_index.core import Settings
from llama_index.llms.ollama import Ollama
from llama_index.core import PromptTemplate
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader
from llama_index.readers.docling import DoclingReader
from llama_index.core.node_parser import MarkdownNodeParser

# Global cache for document indices
document_cache = {}
llm_lock = threading.Lock()

def initialize_llm():
    """Initialize the LLM client"""
    with llm_lock:
        initialized_llm = Ollama(model="llama3.2", request_timeout=120.0)
        return initialized_llm

def clear_document_from_cache(document_key):
    """Remove a document from the cache to free memory"""
    if document_key in document_cache:
        del document_cache[document_key]
        gc.collect()

def process_pdf_document(file_path, document_id):
    """Process a PDF document and create a query engine for it"""
    document_key = f"doc_{document_id}"
    
    if document_key in document_cache:
        return document_cache[document_key]
    
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Copy the file to the temp directory
            temp_file_path = os.path.join(temp_dir, os.path.basename(file_path))
            with open(file_path, "rb") as src_file, open(temp_file_path, "wb") as dst_file:
                dst_file.write(src_file.read())
            
            # Process the document
            reader = DoclingReader()
            directory_loader = SimpleDirectoryReader(
                input_dir=temp_dir,
                file_extractor={".pdf": reader},
            )
            
            documents = directory_loader.load_data()
            
            loaded_llm = initialize_llm()
            embedding_model = HuggingFaceEmbedding(model_name="BAAI/bge-large-en-v1.5", trust_remote_code=True)
            
            Settings.embed_model = embedding_model
            markdown_parser = MarkdownNodeParser()
            index = VectorStoreIndex.from_documents(
                documents=documents,
                transformations=[markdown_parser],
                show_progress=True
            )
            
            Settings.llm = loaded_llm
            query_engine = index.as_query_engine(streaming=True)
            
            custom_qa_prompt = (
                "Context information is below.\n"
                "---------------------\n"
                "{context_str}\n"
                "---------------------\n"
                "Given the context information above I want you to think step by step to answer the query in a highly precise and crisp manner focused on the final answer, in case you don't know the answer say 'I don't know!'.\n"
                "Query: {query_str}\n"
                "Answer: "
            )
            prompt_template = PromptTemplate(custom_qa_prompt)
            
            query_engine.update_prompts({"response_synthesizer:text_qa_template": prompt_template})
            
            document_cache[document_key] = query_engine
            return query_engine
            
    except Exception as error:
        print(f"An error occurred: {error}")
        return None

def query_document(document_id, file_path, query_text):
    """Query a document with the given text"""
    query_engine = process_pdf_document(file_path, document_id)
    
    if not query_engine:
        return "Error: Could not process the document."
    
    try:
        response = query_engine.query(query_text)
        return response.response
    except Exception as e:
        return f"Error processing query: {str(e)}"

def extract_pdf_content(file_path, output_dir=None):
    """Extract content from a PDF file including text, tables, and images"""
    if output_dir is None:
        output_dir = tempfile.mkdtemp()
    
    images_dir = os.path.join(output_dir, "extracted_images")
    text_dir = os.path.join(output_dir, "extracted_text")
    readme_dir = os.path.join(output_dir, "extracted_readme")
    
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(text_dir, exist_ok=True)
    os.makedirs(readme_dir, exist_ok=True)
    
    # Load spaCy model
    try:
        nlp = spacy.load("fr_core_news_sm")
    except:
        # If model not found, download it
        import subprocess
        subprocess.run(["python", "-m", "spacy", "download", "fr_core_news_sm"])
        nlp = spacy.load("fr_core_news_sm")
    
    # Split PDF into pages
    pdf_reader = PdfReader(file_path)
    base = os.path.splitext(os.path.basename(file_path))[0]
    output_files = []
    
    for i, page in enumerate(pdf_reader.pages):
        writer = PdfWriter()
        writer.add_page(page)
        page_file_path = os.path.join(output_dir, f"{base}_page_{i+1}.pdf")
        with open(page_file_path, "wb") as f_out:
            writer.write(f_out)
        output_files.append(page_file_path)
    
    # Process each page
    extracted_content = []
    
    for idx, page_file in enumerate(output_files, 1):
        content = extract_content_from_page(page_file, idx, nlp, images_dir)
        save_txt_and_md(base, idx, content, text_dir, readme_dir)
        extracted_content.append(content)
    
    return {
        "output_dir": output_dir,
        "pages": extracted_content
    }

def extract_content_from_page(pdf_path, page_num, nlp, images_dir):
    """Extract content from a single PDF page"""
    result = {
        "text": "",
        "tables": [],
        "tables_desc": [],
        "images": [],
        "images_desc": []
    }
    
    with pdfplumber.open(pdf_path) as pdf:
        if len(pdf.pages) < 1:
            return result
        
        page = pdf.pages[0]
        result["text"] = page.extract_text() or ""
        
        # Extract tables
        tables = page.extract_tables()
        for tab in tables:
            result["tables"].append(tab)
            desc = summarize_table(tab)
            result["tables_desc"].append(desc)
        
        # Extract images
        for i, img in enumerate(page.images):
            bbox = (img["x0"], img["top"], img["x1"], img["bottom"])
            page_image = page.to_image(resolution=200)
            pil_img = page_image.original  # PIL.Image object (entire page)

            width_pixels, height_pixels = pil_img.size
            scale = width_pixels / float(page.width)

            # Calculate bounding box in pixels
            pixel_bbox = tuple(int(coord * scale) for coord in bbox)

            # Safety: clamp values
            left = max(0, pixel_bbox[0])
            upper = max(0, pixel_bbox[1])
            right = min(width_pixels, pixel_bbox[2])
            lower = min(height_pixels, pixel_bbox[3])

            if right > left and lower > upper:
                cropped_image = pil_img.crop((left, upper, right, lower))
                img_filename = os.path.join(images_dir, f"page{page_num}_img{i+1}.png")
                cropped_image.save(img_filename)
                result["images"].append(img_filename)
                img_caption = extract_image_caption(page, bbox, nlp)
                result["images_desc"].append(img_caption)
    
    return result

def summarize_table(table):
    """Provide a simple summary of a table: number of rows, columns, first elements"""
    if not table or not any(table):
        return "Empty table"
    
    num_rows = len(table)
    num_cols = len(table[0])
    head = table[0]
    description = f"Table with {num_rows} rows and {num_cols} columns. Header: {head}."
    
    return description

def extract_image_caption(page, bbox, nlp):
    """Extract a caption from below an image"""
    # Extract a rectangle below the image to find accompanying text
    # (Heuristic: box just below the image, 30 points in height)
    cap_top = bbox[3]
    cap_bot = cap_top + 30
    
    try:
        crop = page.within_bbox((bbox[0], cap_top, bbox[2], cap_bot))
        text = crop.extract_text() if crop else ""
        
        if not text:
            return "Extracted image (no description detected)"
        
        doc = nlp(text)
        relevant = " ".join([sent.text for sent in doc.sents])
        
        return relevant or "Extracted image"
    except Exception:
        return "Extracted image"

def save_txt_and_md(base_name, page_number, content, text_dir, readme_dir):
    """Generate .txt and .md versions in separate folders"""
    txt_filename = os.path.join(text_dir, f"{base_name}_page_{page_number}.txt")
    md_filename = os.path.join(readme_dir, f"{base_name}_page_{page_number}.md")

    # 1. Plain text file
    with open(txt_filename, "w", encoding="utf-8") as f:
        f.write(content["text"])
        for i, tab in enumerate(content["tables"]):
            f.write("\n\n----------\nTable %d\n----------\n" % (i+1))
            for row in tab:
                f.write(" | ".join(str(cell) for cell in row) + "\n")

    # 2. Structured markdown file
    with open(md_filename, "w", encoding="utf-8") as f:
        f.write(f"# Page {page_number}\n\n")
        f.write("## Main Text\n\n")
        f.write(content["text"] + "\n\n")

        for i, (tab, desc) in enumerate(zip(content["tables"], content["tables_desc"])):
            f.write(f"## Table {i+1}\n")
            f.write(f"- Description: {desc}\n")
            f.write("\n| " + " | ".join(str(cell) if cell is not None else "" for cell in tab[0]) + " |\n")
            f.write("|" + " --- |" * len(tab[0]) + "\n")
            for row in tab[1:]:
                f.write("| " + " | ".join(str(cell) if cell is not None else "" for cell in row) + " |\n")
            f.write("\n")

        for i, (img, desc) in enumerate(zip(content["images"], content["images_desc"])):
            f.write(f"## Image {i+1}\n")
            f.write(f"![Extracted image]({img})\n")
            f.write(f"- Automatic caption: {desc}\n\n")
