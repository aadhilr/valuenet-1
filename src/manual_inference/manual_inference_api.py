import os
import pickle

import torch
from flask import Flask, make_response, abort, request
from flask_cors import CORS

from spacy.lang.en import English

from config import read_arguments_manual_inference
from intermediate_representation import semQL
from manual_inference.helper import _tokenize_question, _inference_semql, _pre_process_values, _semql_to_sql, \
    _execute_query_postgresql, _get_schemas_spider, _get_schemas_cordis, _is_cordis_or_spider, _execute_query_sqlite
from model.model import IRNet
from named_entity_recognition.database_value_finder.database_value_finder_postgresql import \
    DatabaseValueFinderPostgreSQL
from named_entity_recognition.database_value_finder.database_value_finder_sqlite import DatabaseValueFinderSQLite
from preprocessing.process_data import process_datas
from preprocessing.utils import merge_data_with_schema
from utils import setup_device, set_seed_everywhere

app = Flask(__name__)
CORS(app)

args = read_arguments_manual_inference()

device, n_gpu = setup_device()
set_seed_everywhere(args.seed, n_gpu)

connection_config = {k: v for k, v in vars(args).items() if k.startswith('database')}

schemas_raw_spider, schemas_dict_spider, schema_path_spider, database_path_spider = _get_schemas_spider()
schemas_raw_cordis, schemas_dict_cordis, schema_path_cordis, database_path_cordis = _get_schemas_cordis()

grammar = semQL.Grammar()
model = IRNet(args, device, grammar)
model.to(device)

# load the pre-trained parameters
model.load_state_dict(torch.load(args.model_to_load))
model.eval()
print("Load pre-trained model from '{}'".format(args.model_to_load))

nlp = English()
tokenizer = nlp.Defaults.create_tokenizer(nlp)

with open(os.path.join(args.conceptNet, 'english_RelatedTo.pkl'), 'rb') as f:
    related_to_concept = pickle.load(f)

with open(os.path.join(args.conceptNet, 'english_IsA.pkl'), 'rb') as f:
    is_a_concept = pickle.load(f)


@app.route('/')
@app.route('/health')
@app.route('/api/health')
def health():
    response = make_response('''


    VVVVVVVV           VVVVVVVV               lllllll                                       NNNNNNNN        NNNNNNNN                             tttt          
    V::::::V           V::::::V               l:::::l                                       N:::::::N       N::::::N                          ttt:::t          
    V::::::V           V::::::V               l:::::l                                       N::::::::N      N::::::N                          t:::::t          
    V::::::V           V::::::V               l:::::l                                       N:::::::::N     N::::::N                          t:::::t          
     V:::::V           V:::::Vaaaaaaaaaaaaa    l::::l uuuuuu    uuuuuu      eeeeeeeeeeee    N::::::::::N    N::::::N    eeeeeeeeeeee    ttttttt:::::ttttttt    
      V:::::V         V:::::V a::::::::::::a   l::::l u::::u    u::::u    ee::::::::::::ee  N:::::::::::N   N::::::N  ee::::::::::::ee  t:::::::::::::::::t    
       V:::::V       V:::::V  aaaaaaaaa:::::a  l::::l u::::u    u::::u   e::::::eeeee:::::eeN:::::::N::::N  N::::::N e::::::eeeee:::::eet:::::::::::::::::t    
        V:::::V     V:::::V            a::::a  l::::l u::::u    u::::u  e::::::e     e:::::eN::::::N N::::N N::::::Ne::::::e     e:::::etttttt:::::::tttttt    
         V:::::V   V:::::V      aaaaaaa:::::a  l::::l u::::u    u::::u  e:::::::eeeee::::::eN::::::N  N::::N:::::::Ne:::::::eeeee::::::e      t:::::t          
          V:::::V V:::::V     aa::::::::::::a  l::::l u::::u    u::::u  e:::::::::::::::::e N::::::N   N:::::::::::Ne:::::::::::::::::e       t:::::t          
           V:::::V:::::V     a::::aaaa::::::a  l::::l u::::u    u::::u  e::::::eeeeeeeeeee  N::::::N    N::::::::::Ne::::::eeeeeeeeeee        t:::::t          
            V:::::::::V     a::::a    a:::::a  l::::l u:::::uuuu:::::u  e:::::::e           N::::::N     N:::::::::Ne:::::::e                 t:::::t    tttttt
             V:::::::V      a::::a    a:::::a l::::::lu:::::::::::::::uue::::::::e          N::::::N      N::::::::Ne::::::::e                t::::::tttt:::::t
              V:::::V       a:::::aaaa::::::a l::::::l u:::::::::::::::u e::::::::eeeeeeee  N::::::N       N:::::::N e::::::::eeeeeeee        tt::::::::::::::t
               V:::V         a::::::::::aa:::al::::::l  uu::::::::uu:::u  ee:::::::::::::e  N::::::N        N::::::N  ee:::::::::::::e          tt:::::::::::tt
                VVV           aaaaaaaaaa  aaaallllllll    uuuuuuuu  uuuu    eeeeeeeeeeeeee  NNNNNNNN         NNNNNNN    eeeeeeeeeeeeee            ttttttttttt  


    Is up and reay for your request!''')

    response.headers["content-type"] = "text/plain"

    return response


@app.route("/question/<database>", methods=["PUT"])
@app.route("/api/question/<database>",
           methods=["PUT"])  # this is a fallback for local usage, as the reverse-proxy on nginx will add this prefix
def pose_question(database):
    """
    Ask a question and get the SemQL, SQL and result, as well as some further information.
    Make sure to provide a proper X-API-Key as header parameter.

    Example (cordis): curl -i -X PUT -H "Content-Type: application/json" -H "X-API-Key: 1234" -d '{"question":"Show me project title and cost of the project with the highest total cost"}'  http://localhost:5000/api/question/cordis_temporary
    Example (spider): curl -i -X PUT -H "Content-Type: application/json" -H "X-API-Key: 1234" -d '{"question":"Which bridge has been built by Zaha Hadid?"}'  http://localhost:5000/api/question/architecture

    @param database: Database to execute this query against.
    @return:
    """

    _verify_api_key()

    question = _get_question_from_payload()

    if _is_cordis_or_spider(database) == 'spider':
        schemas_raw = schemas_raw_spider
        schemas_dict = schemas_dict_spider
        db_value_finder = DatabaseValueFinderSQLite(database_path_spider, database, schema_path_spider)
        execute_query_func = lambda sql_to_execute: _execute_query_sqlite(sql_to_execute, database_path_spider, database)
    else:
        schemas_raw = schemas_raw_cordis
        schemas_dict = schemas_dict_cordis
        db_value_finder = DatabaseValueFinderPostgreSQL(database, schema_path_cordis, connection_config)
        execute_query_func = lambda sql_to_execute: _execute_query_postgresql(sql_to_execute, database, connection_config)

    input_data = {
        'question': question,
        'query': 'DUMMY',
        'db_id': database,
        'question_toks': _tokenize_question(tokenizer, question)
    }

    print(f"question has been tokenized to : {input_data['question_toks']}")

    data, table = merge_data_with_schema(schemas_raw, [input_data])

    pre_processed_data = process_datas(data, related_to_concept, is_a_concept)

    pre_processed_with_values = _pre_process_values(pre_processed_data[0], db_value_finder)

    print(f"we found the following potential values in the question: {input_data['values']}")

    prediction, example = _inference_semql(pre_processed_with_values, schemas_dict, model)

    print(f"Results from schema linking (question token types): {example.src_sent}")
    print(f"Results from schema linking (column types): {example.col_hot_type}")

    print(f"Predicted SemQL-Tree: {prediction['model_result']}")
    sql = _semql_to_sql(prediction, schemas_dict)

    print(f"Transformed to SQL: {sql}")
    result = execute_query_func(sql)

    print(f"Executed on the database '{database}'. First 10 Results: ")
    for row in result[:10]:
        print(row)

    result_max_100_rows = result[:min(len(result), 100)]

    return {
        'question': question,
        'question_tokenized': input_data['question_toks'],
        'potential_values_found_in_db': input_data['values'],
        'sem_ql': prediction['model_result'],
        'sql': sql,
        'result': result_max_100_rows
    }


def _verify_api_key():
    api_key = request.headers.get('X-API-Key', default='No API Key provided', type=str)
    print(f'provided API-Key is {api_key}')
    if not args.api_key == api_key:
        print('Invalid API-Key! Abort with 403')
        abort(403, description="Please provide a valid API Key")


def _get_question_from_payload():
    data = request.get_json(silent=True)
    print(data)
    if data and data['question']:
        return data['question']
    else:
        abort(400, description="Please specify a question, e.g. POST { question: 'Whats the question?' }")


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0')
