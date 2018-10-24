import json
import os
import shelve
import threading
import time
import traceback
from collections import defaultdict
from idseq_dag.engine.pipeline_step import PipelineStep
import idseq_dag.util.command as command
import idseq_dag.util.count as count
import idseq_dag.util.s3 as s3
import idseq_dag.util.m8 as m8

from idseq_dag.steps.run_assembly import PipelineStepRunAssembly

MIN_REF_FASTA_SIZE = 25
MIN_ASEEMBLED_CONTIG_SIZE = 25

class PipelineStepBlastContigs(PipelineStep):
    '''
        BLAST the assembled results to the candidate accessions
    '''
    def run(self):
        '''
            1. summarize hits
            2. built blast index
            3. blast assembled contigs to the index
            4. update the summary
        '''
        (align_m8, deduped_m8, hit_summary, orig_counts) = self.input_files_local[0]
        assembled_contig, _assembled_scaffold, bowtie_sam, _contig_stats = self.input_files_local[1]
        reference_fasta = self.input_files_local[2][0]

        (blast_m8, refined_m8, refined_hit_summary, refined_counts, contig_summary_json) = self.output_files_local()
        db_type = self.additional_attributes["db_type"]
        if os.path.getsize(assembled_contig) < MIN_ASEEMBLED_CONTIG_SIZE or \
            os.path.getsize(reference_fasta) < MIN_REF_FASTA_SIZE:
                # No assembled results or refseq fasta available
                command.execute(f"echo ' ' > {blast_m8}")
                command.execute(f"cp {deduped_m8} {refined_m8}")
                command.execute(f"cp {hit_summary} {refined_hit_summary}")
                command.execute(f"cp {orig_counts} {refined_counts}")
                command.execute("echo '{}' > " + contig_summary_json)
                return

        (read_dict, accession_dict, _selected_genera) = m8.summarize_hits(hit_summary)
        top_entry_m8 = blast_m8.replace(".m8", ".top.m8")
        PipelineStepBlastContigs.run_blast(assembled_contig, reference_fasta,
                                           db_type, blast_m8, top_entry_m8)
        read2contig = {}
        contig_stats = defaultdict(int)
        PipelineStepRunAssembly.generate_info_from_sam(bowtie_sam, read2contig, contig_stats)

        (updated_read_dict, read2blastm8, contig2lineage) = self.update_read_dict(read2contig,
                                                                                  top_entry_m8,
                                                                                  read_dict,
                                                                                  accession_dict)
        self.generate_m8_and_hit_summary(updated_read_dict, read2blastm8,
                                         hit_summary, deduped_m8,
                                         refined_hit_summary, refined_m8)

        # Generating taxon counts based on updated results
        lineage_db = s3.fetch_from_s3(
            self.additional_files["lineage_db"],
            self.ref_dir_local,
            allow_s3mi=True)
        deuterostome_db = None
        evalue_type = 'raw'
        if self.additional_files.get("deuterostome_db"):
            deuterostome_db = s3.fetch_from_s3(self.additional_files["deuterostome_db"],
                                               self.ref_dir_local, allow_s3mi=True)
        m8.generate_taxon_count_json_from_m8(refined_m8, refined_hit_summary,
                                             evalue_type, db_type.upper(),
                                             lineage_db, deuterostome_db, refined_counts)
        # generate contig stats at genus/species level
        contig_taxon_summary = self.generate_taxon_summary(read2contig, contig2lineage, updated_read_dict, db_type)
        with open(contig_summary_json, 'w') as contig_outf:
            json.dump(contig_taxon_summary, contig_outf)

        # Upload additional file
        self.additional_files_to_upload.append(top_entry_m8)

    @staticmethod
    def generate_taxon_summary(read2contig, contig2lineage, read_dict, db_type):
        # Return an array with
        # { taxid: , tax_level:, contig_counts: { 'contig_name': <count>, .... } }
        genus_summary = defaultdict(lambda: defaultdict(int))
        species_summary = defaultdict(lambda: defaultdict(int))
        for read_id, read_info in read_dict.items():
            contig = read2contig.get(read_id)
            if contig and contig != '*':
                species_taxid, genus_taxid = contig2lineage[contig]
                species_summary[species_taxid][contig] += 1
                genus_summary[genus_taxid][contig] += 1
            else:
                # not mapping to a contig
                species_taxid, genus_taxid = read_info[4:6]
                species_summary[species_taxid]['*'] += 1
                genus_summary[genus_taxid]['*'] += 1
        # constructing the array for output
        output_array = []
        for idx, summary in enumerate([species_summary, genus_summary]):
            tax_level = idx + 1
            for taxid, contig_counts in summary.items():
                entry = { 'taxid': taxid, 'tax_level': tax_level,
                          'count_type': db_type.upper(), 'contig_counts': contig_counts}
                output_array.append(entry)
        return output_array

    @staticmethod
    def generate_m8_and_hit_summary(consolidated_dict, read2blastm8,
                                    hit_summary, deduped_m8,
                                    refined_hit_summary, refined_m8):
        ''' generate new m8 and hit_summary based on consolidated_dict and read2blastm8 '''
        # Generate new hit summary
        with open(refined_hit_summary, 'w') as rhsf:
            with open(hit_summary, 'r', encoding='utf-8') as hsf:
                for line in hsf:
                    read_id = line.rstrip().split("\t")[0]
                    read = consolidated_dict[read_id]
                    output_str = "\t".join(read)
                    rhsf.write(output_str + "\n")
        # Generate new M8
        with open(refined_m8, 'w') as rmf:
            with open(deduped_m8, 'r', encoding='utf-8') as mf:
                for line in mf:
                    read_id = line.rstrip().split("\t")[0]
                    m8_line = read2blastm8.get(read_id)
                    if m8_line:
                        m8_fields = m8_line.split("\t")
                        m8_fields[0] = read_id
                        rmf.write("\t".join(m8_fields))
                    else:
                        rmf.write(line)

    @staticmethod
    def update_read_dict(read2contig, blast_top_m8, read_dict, accession_dict):
        consolidated_dict = read_dict
        read2blastm8 = {}
        contig2accession = {}
        contig2lineage = {}

        for contig_id, accession_id, _percent_id, _alignment_length, e_value, _bitscore, line in m8.iterate_m8(blast_top_m8):
            contig2accession[contig_id] = (accession_id, line)
            contig2lineage = accession_dict[accession_id]

        for read_id, contig_id in read2contig.items():
            (accession, m8_line) = contig2accession.get(contig_id, (None, None))
            if accession:
                (species_taxid, genus_taxid) = accession_dict[accession]
                consolidated_dict[read_id] += [contig_id, accession, species_taxid, genus_taxid]
                consolidated_dict[read_id][2] = species_taxid
            if m8_line:
                read2blastm8[read_id] = m8_line
        return (consolidated_dict, read2blastm8, contig2lineage)

    @staticmethod
    def run_blast(assembled_contig, reference_fasta, db_type, blast_m8, top_entry_m8):
        blast_index_path = os.path.join(os.path.dirname(blast_m8), f"{db_type}_blastindex")
        blast_type = 'nucl'
        blast_command = 'blastn'
        if db_type == 'nr':
            blast_type = 'prot'
            blast_command = 'blastx'
        command.execute(f"makeblastdb -in {reference_fasta} -dbtype {blast_type} -out {blast_index_path}")
        command.execute(f"{blast_command} -query {assembled_contig} -db {blast_index_path} -out {blast_m8} -outfmt 6 -num_alignments 5 -num_threads 32")
        # further processing of getting the top m8 entry for each contig.
        PipelineStepBlastContigs.get_top_m8(blast_m8, top_entry_m8)

    @staticmethod
    def get_top_m8(orig_m8, top_entry_m8):
        ''' Get top m8 file entry for each read from orig_m8 and output to top_entry_m8 '''
        with open(top_entry_m8, 'w') as top_m8f:
            top_line = None
            top_bitscore = 0
            current_read_id = None
            for read_id, _accession_id, _percent_id, _alignment_length, e_value, bitscore, line in m8.iterate_m8(orig_m8):
                # Get the top entry of each read_id based on the bitscore
                if read_id != current_read_id:
                    # Different batch start
                    if current_read_id: # Not the first line
                        top_m8f.write(top_line)
                    current_read_id = read_id
                    top_line = line
                    top_bitscore = bitscore
                elif bitscore > top_bitscore:
                    top_bitscore = bitscore
                    top_line = line
            top_m8f.write(top_line)









