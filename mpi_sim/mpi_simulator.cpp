#include "mpi_simulator.hpp"

void MpiInterface::initialize_chunks(MpiSimulatorChunk* chunk, int num_chunks){
    master_chunk = chunk;
    num_remote_chunks = num_chunks;

    cout << "C++: Initializing remote processes." << endl;
    MPI_Comm everyone;

    int argc = 0;
    char** argv;

    cout << "Master initing MPI..." << endl;
    MPI_Init(&argc, &argv);
    cout << "Master finished initing MPI." << endl;

    cout << "Master spawning " << num_remote_chunks << " children..." << endl;

    MPI_Comm_spawn("mpi_sim_worker", MPI_ARGV_NULL, num_remote_chunks,
             MPI_INFO_NULL, 0, MPI_COMM_SELF, &everyone,
             MPI_ERRCODES_IGNORE);

    cout << "Master finished spawning children." << endl;

    mpi::intercommunicator intercomm(everyone, mpi::comm_duplicate);
    comm = intercomm.merge(false);

    int buflen = 512;
    char name[buflen];
    MPI_Get_processor_name(name, &buflen);

    cout << "Master host: " << name << endl;
    cout << "Master rank in merged communicator: " << comm.rank() << " (should be 0)." << endl;

    float dt = master_chunk->dt;
    string chunk_label;

    int tag = 1;

    for(int i = 0; i < num_remote_chunks; i++){
        stringstream s;
        s << "Chunk " << i + 1;
        chunk_label = s.str();

        comm.send(i + 1, tag, chunk_label);
        comm.send(i + 1, tag, dt);
    }
}

void MpiInterface::add_signal(int component, key_type key, string label, Matrix* data){
    int tag = 1;

    comm.send(component, tag, add_signal_flag);

    comm.send(component, tag, key);
    comm.send(component, tag, label);
    comm.send(component, tag, *data);
}

void MpiInterface::add_op(int component, string op_string){
    int tag = 1;

    comm.send(component, tag, add_op_flag);

    comm.send(component, tag, op_string);
}

void MpiInterface::add_probe(int component, key_type probe_key, key_type signal_key, float period){
    int tag = 1;

    comm.send(component, tag, add_probe_flag);

    comm.send(component, tag, probe_key);
    comm.send(component, tag, signal_key);
    comm.send(component, tag, period);
}

void MpiInterface::finalize(){
    cout << "C++: Finalizing master chunk." << endl;

    master_chunk->setup_mpi_waits();

    map<int, MPISend*>::iterator send_it;
    for(send_it = master_chunk->mpi_sends.begin(); send_it != master_chunk->mpi_sends.end(); ++send_it){
        send_it->second->comm = &comm;
    }

    map<int, MPIRecv*>::iterator recv_it;
    for(recv_it = master_chunk->mpi_recvs.begin(); recv_it != master_chunk->mpi_recvs.end(); ++recv_it){
        recv_it->second->comm = &comm;
    }

    int tag = 1;

    for(int i = 0; i < num_remote_chunks; i++){
        comm.send(i + 1, tag, stop_flag);
    }
}

void MpiInterface::run_n_steps(int steps){
    cout << "Master sending simulation signal." << endl;
    broadcast(comm, steps, 0);

    cout << "Master starting simulation: " << steps << " steps." << endl;

    master_chunk->run_n_steps(steps);

    comm.barrier();

    cout << "Finished simulation." << endl;
}

void MpiInterface::gather_probe_data(map<key_type, vector<Matrix*>*>& probe_data,
                                     map<int, int>& probe_counts){
    key_type probe_key;
    vector<Matrix*>* data = NULL;
    map<int, int>::iterator count_it;
    int chunk_index, probe_count;

    cout << "Master gathering probe data from children..." << endl;

    for(count_it = probe_counts.begin(); count_it != probe_counts.end(); ++count_it){
        chunk_index = count_it->first;
        probe_count = count_it->second;

        if(chunk_index > 0){
            for(unsigned i = 0; i < probe_count; i++){
                data = new vector<Matrix*>();

                cout << "Master receiving probe from chunk " << chunk_index;
                comm.recv(chunk_index, 3, probe_key);
                cout << " with key " << probe_key << "..." << endl;
                comm.recv(chunk_index, 3, *data);
                cout << "Done receiving probe data." << endl;

                probe_data[probe_key] = data;
            }
        }
    }

    cout << "Master done gathering probe data from children." << endl;

    comm.barrier();
}

void MpiInterface::finish_simulation(){
    MPI_Finalize();
}