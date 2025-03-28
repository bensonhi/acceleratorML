import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
from typing import List
from tqdm import tqdm
import pickle
import at
import os

import utils
from sklearn.preprocessing import StandardScaler
from multiprocessing import Manager

class OrbitCorrectionNN(nn.Module):
    def __init__(self, n_elements: int, n_correctors: int, dropout_rate: float = 0.5):
        """Neural Network for orbit correction using layers"""
        super(OrbitCorrectionNN, self).__init__()

        # Calculate input size including initial corrector values
        input_size = n_elements * 2 + n_correctors

        self.network = nn.Sequential(
            nn.Linear(input_size, 1024),
            nn.BatchNorm1d(1024),
            nn.LeakyReLU(negative_slope=0.01),

            nn.Linear(1024, 512),
            nn.BatchNorm1d(512),
            nn.LeakyReLU(negative_slope=0.01),

            nn.Linear(512, n_correctors)
        )

        self._initialize_weights()

    def _initialize_weights(self):
        """Initialize network weights using Xavier initialization"""
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.kaiming_normal_(m.weight, nonlinearity='leaky_relu')
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    def forward(self, x):
        return self.network(x)


class OrbitCorrector:
    def __init__(self, base_ring, device='cpu', dropout_rate=0.5, weight_decay=1e-5):
        """Initialize orbit corrector with ML model"""
        self.base_ring = base_ring
        self.device = device
        self.weight_decay = weight_decay

        # Initialize scalers
        self._init_scalers()

        # Setup model parameters
        self._setup_model_params()

        # Initialize model and optimizer
        self.model = OrbitCorrectionNN(
            n_elements=self.n_true_trajectory_inputs,
            n_correctors=self.n_correctors,
            dropout_rate=dropout_rate
        ).to(device)

        self.optimizer = optim.Adam(self.model.parameters(), weight_decay=weight_decay)
        self.criterion = nn.MSELoss()

    def _init_scalers(self):
        """Initialize data scalers"""
        self.trajectory_scaler = StandardScaler()
        self.corrector_scaler = StandardScaler()
        self.initial_corrector_scaler = StandardScaler()

    def _setup_model_params(self):
        """Setup model parameters from base ring"""
        bpm_readings, true_trajectory = utils.getBPMreading(self.base_ring)
        self.n_true_trajectory_inputs = len(bpm_readings)

        self.hcm = utils.getCorrectorStrengths(self.base_ring, 'x')
        self.vcm = utils.getCorrectorStrengths(self.base_ring, 'y')
        self.n_correctors = len(self.hcm) + len(self.vcm)

    def fit_scalers(self, train_data):
        """Fit the scalers on training data"""
        trajectories = np.vstack([
            data[0].reshape(-1, 2) if data[0].ndim == 1 else data[0]
            for data in train_data
        ])
        corrections = np.vstack([data[2] for data in train_data])
        initial_corrections = np.vstack([data[1] for data in train_data])

        self.trajectory_scaler.fit(trajectories)
        self.corrector_scaler.fit(corrections)
        self.initial_corrector_scaler.fit(initial_corrections)

    def prepare_input(self, true_trajectory: np.ndarray, initial_correctors: np.ndarray) -> torch.Tensor:
        """
        Prepare trajectory and initial corrector data for model input
        Args:
            true_trajectory: Array of shape (n_elements, 2) with x,y positions
            initial_correctors: Array of initial corrector values
        """
        # Reshape trajectory to 2D array if needed
        if true_trajectory.ndim == 1:
            true_trajectory = true_trajectory.reshape(-1, 2)

        # Transform using fitted scalers
        normalized_traj = self.trajectory_scaler.transform(true_trajectory)
        normalized_init_corr = self.initial_corrector_scaler.transform(
            initial_correctors.reshape(1, -1)
        )

        # Concatenate and flatten for model input
        x = np.concatenate([normalized_traj.flatten(), normalized_init_corr.flatten()])

        return torch.FloatTensor(x).to(self.device)

    def prepare_target(self, target_corrections: np.ndarray) -> torch.Tensor:
        """Normalize target corrections using fitted scaler"""
        normalized_corrections = self.corrector_scaler.transform(
            target_corrections.reshape(1, -1) if target_corrections.ndim == 1
            else target_corrections
        )
        return torch.FloatTensor(normalized_corrections).to(self.device)


    def train(self, train_data, val_seeds, epochs=100, batch_size=32, augment_interval=1000):
        """
        Train the neural network with normalized data and progressive augmentation
        
        Args:
            train_data: Initial training data
            val_seeds: Seeds for validation
            epochs: Total number of epochs
            batch_size: Batch size for training
            augment_interval: Interval (in epochs) to check for augmentation
        """
        # First fit the scalers on training data
        self.fit_scalers(train_data)

        train_losses = []
        val_losses = []
        
        # Track the best validation performance
        best_loss_improvement = 0
        best_model_path = None
        
        # For tracking augmentation status
        last_augmentation_epoch = 0
        augmentation_counter = 0
        has_improved_since_last_augmentation = False
        
        # Current training data - will grow over time
        current_train_data = train_data.copy()
        
        # Create directory for saving models if it doesn't exist
        os.makedirs('saved_models', exist_ok=True)

        for epoch in range(epochs):
            self.model.train()
            epoch_loss = 0
            batch_count = 0

            # Training loop
            for i in range(0, len(current_train_data), batch_size):
                batch = current_train_data[i:i + batch_size]

                # Prepare normalized inputs and targets
                inputs = torch.stack([
                    self.prepare_input(b[0], b[1]) for b in batch
                ])
                target_corrections = torch.stack([
                    self.prepare_target(b[2])[0] for b in batch
                ])

                self.optimizer.zero_grad()
                predicted_corrections = self.model(inputs)

                loss = self.criterion(predicted_corrections, target_corrections)
                loss.backward()
                self.optimizer.step()

                epoch_loss += loss.item()
                batch_count += 1

            avg_train_loss = epoch_loss / batch_count
            train_losses.append(avg_train_loss)

            # Validation
            if(epoch+1)%50==0:
                self.model.eval()
                with torch.no_grad():
                    val_results = self.validate(val_seeds)
                    
                    # Calculate average validation metrics
                    avg_loss_improvement = np.mean([r['loss_improvement'] for r in val_results])
                    
                    # Save if it's the best model so far
                    if avg_loss_improvement > best_loss_improvement:
                        best_loss_improvement = avg_loss_improvement
                        model_path = f'saved_models/best_loss_model_epoch_{epoch+1}_improvement_{avg_loss_improvement:.2f}pct.pt'
                        self.save_model_and_scalers(model_path)
                        best_model_path = model_path
                        has_improved_since_last_augmentation = True
                        print(f"New best loss improvement model saved: {avg_loss_improvement:.2f}%")
                    
                    val_losses.append(avg_loss_improvement)
            
            # Check for data augmentation every augment_interval epochs
            if (epoch + 1) % augment_interval == 0:
                # Only augment if we've found a better model since the last augmentation
                if has_improved_since_last_augmentation and best_model_path is not None:
                    print(f"=== Augmenting training data at epoch {epoch+1} ===")
                    print(f"Using best model: {best_model_path} ({best_loss_improvement:.2f}% improvement)")
                    
                    # Determine the seed range based on the original training data
                    if isinstance(val_seeds, range):
                        seed_start = 0  # Assuming we always start from 0
                        seed_end = val_seeds.start - 1
                    else:
                        # Fallback if val_seeds is not a range
                        seed_start = 0
                        seed_end = 16000 - 1  # Default to what was in the original code
                    
                    # Generate augmented data using the best model
                    new_augmented_data = self.generate_augmented_training_data(
                        seed_range=(seed_start, seed_end),
                        model_path=best_model_path,
                        num_workers=12  # Use 12 cores
                    )
                    
                    # Add the new augmented data to our training set
                    augmentation_counter += 1
                    current_train_data = np.concatenate([current_train_data, new_augmented_data])
                    
                    print(f"Dataset size increased: {len(current_train_data) - len(new_augmented_data)} -> {len(current_train_data)}")
                    
                    # Reset the flag so we only augment on improvement
                    has_improved_since_last_augmentation = False
                    last_augmentation_epoch = epoch + 1
                    
                    # Re-fit the scalers on the expanded dataset
                    self.fit_scalers(current_train_data)
                else:
                    print(f"=== No model improvement since last augmentation at epoch {epoch+1} ===")
                    print(f"Continuing training without augmentation")

            if (epoch + 1) % 10 == 0:
                print(f'Epoch {epoch+1}/{epochs}:')
                print(f'Training Loss: {avg_train_loss:.6f}')
                print(f'Dataset size: {len(current_train_data)} samples')
                print(f'Augmentations so far: {augmentation_counter}')

        print(f"Training completed with {augmentation_counter} augmentations")
        print(f"Final dataset size: {len(current_train_data)} samples")
        return train_losses, val_losses
    
    def save_model_and_scalers(self, filepath):
        """Save model weights and fitted scalers"""
        torch.save({
            'model_state_dict': self.model.state_dict(),
            'trajectory_scaler': self.trajectory_scaler,
            'corrector_scaler': self.corrector_scaler,
            'initial_corrector_scaler': self.initial_corrector_scaler
        }, filepath)
        
    def load_model_and_scalers(self, filepath):
        """Load model weights and fitted scalers from file"""
        try:
            # Option 1: Add StandardScaler to safe globals (recommended approach)
            import torch.serialization
            
            # Add StandardScaler to the list of safe globals
            torch.serialization.add_safe_globals([StandardScaler])
            
            # Then load the checkpoint
            checkpoint = torch.load(filepath, map_location=self.device)
            
        except Exception as e:
            # Option 2: Fallback to weights_only=False (less secure but works with older PyTorch)
            print(f"Error with safe loading: {e}")
            print("Falling back to weights_only=False loading method")
            checkpoint = torch.load(filepath, map_location=self.device, weights_only=False)
        
        # Load model state
        self.model.load_state_dict(checkpoint['model_state_dict'])
        
        # Load scalers
        self.trajectory_scaler = checkpoint['trajectory_scaler']
        self.corrector_scaler = checkpoint['corrector_scaler']
        self.initial_corrector_scaler = checkpoint['initial_corrector_scaler']
        
        print(f"Model and scalers loaded from {filepath}")



    def validate_parallel(self, val_seeds, num_workers=8):
        """Test trained model on test seeds using parallel processing
        
        Args:
            val_seeds: List or range of seed numbers to validate
            num_workers: Number of CPU cores to use
            
        Returns:
            List of dictionaries with validation results for each seed
        """
        import os
        import multiprocessing
        from tqdm import tqdm
        
        # Save current model state to a temporary file for workers to load
        tmp_model_path = os.path.join('saved_models', f'tmp_model_for_validation.pt')
        self.save_model_and_scalers(tmp_model_path)
        
        # Create a shared manager for progress tracking
        manager = multiprocessing.Manager()
        progress_dict = manager.dict()
        progress_dict['completed'] = 0
        progress_dict['successful'] = 0
        
        # Create seed batches for parallel processing
        total_seeds = len(val_seeds) if not isinstance(val_seeds, range) else val_seeds.stop - val_seeds.start
        seed_list = list(val_seeds)
        batch_size = max(1, len(seed_list) // num_workers)
        seed_batches = []
        
        for i in range(0, len(seed_list), batch_size):
            end = min(i + batch_size, len(seed_list))
            seed_batches.append(seed_list[i:end])
        
        print(f"Validating {total_seeds} seeds using {num_workers} workers")
        
        # Execute validation in parallel
        all_results = []
        with tqdm(total=total_seeds, desc="Validating model", unit="seed") as pbar:
            with multiprocessing.Pool(processes=min(num_workers, len(seed_batches))) as pool:
                # Prepare worker arguments
                worker_args = [(batch, tmp_model_path, self.base_ring, progress_dict) 
                              for batch in seed_batches]
                
                # Start all worker processes asynchronously
                result_objects = [pool.apply_async(_validate_worker_process, args=(arg,)) 
                                 for arg in worker_args]
                
                # Monitor progress while workers are running
                last_update = 0
                while True:
                    # Check if all workers are done
                    if all(result.ready() for result in result_objects):
                        break
                    
                    # Update progress bar based on shared counter
                    current = progress_dict['completed']
                    if current > last_update:
                        pbar.update(current - last_update)
                        last_update = current
                        
                        # Update additional stats
                        if current > 0:
                            success_rate = 100 * progress_dict['successful'] / current
                            pbar.set_postfix({
                                "Completed": current, 
                                "Success %": f"{success_rate:.1f}%"
                            })
                    
                    # Don't hog CPU with constant checking
                    import time
                    time.sleep(0.1)
                
                # Process results from all workers when complete
                for result_obj in result_objects:
                    worker_results = result_obj.get()
                    all_results.extend(worker_results)
        
        # Clean up temporary model file
        if os.path.exists(tmp_model_path):
            os.remove(tmp_model_path)
        
        # Print summary statistics
        print("total rms improvement:" + str(np.mean([r['rms_improvement'] for r in all_results])) + "%")
        print("expected rms improvement:" + str(np.mean([r['expected rms_improvement'] for r in all_results])) + "%")
        print("total loss improvement:" + str(np.mean([r['loss_improvement'] for r in all_results])) + "%")
        print("expected loss improvement:" + str(np.mean([r['expected loss_improvement'] for r in all_results])) + "%")
        
        return all_results

    def predict_corrections(self, true_trajectory: List[np.ndarray], initial_correctors: np.ndarray) -> np.ndarray:
        """Predict corrector values with proper denormalization"""
        self.model.eval()
        with torch.no_grad():
            true_trajectory_inputs = self.prepare_input(true_trajectory, initial_correctors)
            true_trajectory_inputs = true_trajectory_inputs.unsqueeze(0)
            predictions = self.model(true_trajectory_inputs)

            # Denormalize predictions
            denormalized_predictions = self.corrector_scaler.inverse_transform(
                predictions.cpu().numpy()
            )
            return denormalized_predictions[0]

    def generate_augmented_training_data(self, seed_range=(1, 100), model_path=None, cache_dir='./data_cache', num_workers=13):
        """
        Generate augmented training data in parallel using multiple CPU cores
        
        Args:
            seed_range: Tuple of (start_seed, end_seed) inclusive
            model_path: Path to the saved model to use for augmentation
            cache_dir: Directory to store cached data
            num_workers: Number of CPU cores to use for parallelization
        
        Returns:
            Augmented training data array
        """
        import os
        from tqdm import tqdm
        import multiprocessing
        import numpy as np
        import time
        
        # Load the model if a path is provided
        if model_path:
            self.load_model_and_scalers(model_path)
            # Extract improvement percentage from model filename
            try:
                improvement = float(model_path.split('improvement_')[1].split('pct.pt')[0])
                improvement_str = f"{improvement:.2f}"
            except:
                improvement_str = "unknown"
        else:
            print("Warning: No model path provided, using current model state")
            improvement_str = "current_model"
        
        # Create cache directory if it doesn't exist
        os.makedirs(cache_dir, exist_ok=True)
        
        # Generate cache filename based on seed range and model improvement
        cache_file = os.path.join(
            cache_dir, 
            f'augmented_data_cache_{seed_range[0]}_{seed_range[1]}_model_imp_{improvement_str}pct.npz'
        )
        
        # Try to load from cache first
        if os.path.exists(cache_file):
            print(f"Loading cached augmented data from {cache_file}")
            cached_data = np.load(cache_file, allow_pickle=True)
            return cached_data['augmented_data']
        
        total_seeds = seed_range[1] - seed_range[0] + 1
        print(f"Generating augmented training data for {total_seeds} seeds ({seed_range[0]} to {seed_range[1]}) using {num_workers} workers")
        
        # Create batches of seeds for parallelization
        batch_size = max(1, total_seeds // num_workers)
        seed_batches = []
        
        for i in range(seed_range[0], seed_range[1] + 1, batch_size):
            end = min(i + batch_size - 1, seed_range[1])
            seed_batches.append((i, end))
        
        # Save current model state to a temporary file for workers to load
        tmp_model_path = os.path.join(cache_dir, f'tmp_model_for_augmentation_{improvement_str}.pt')
        self.save_model_and_scalers(tmp_model_path)
        
        # Create a shared counter for progress tracking
        manager = Manager()
        progress_dict = manager.dict()
        progress_dict['completed'] = 0
        progress_dict['successful'] = 0
        
        # Run workers with shared progress tracking
        augmented_data = []
        start_time = time.time()
        
        # Create a progress bar for the overall process
        with tqdm(total=total_seeds, desc="Augmenting data", unit="seed") as pbar:
            with multiprocessing.Pool(processes=min(num_workers, len(seed_batches))) as pool:
                # Prepare worker arguments with progress_dict
                worker_args = [(batch[0], batch[1], tmp_model_path, self.base_ring, progress_dict) 
                              for batch in seed_batches]
                
                # Start all worker processes asynchronously
                result_objects = [pool.apply_async(_augment_worker_process, args=(arg,)) 
                                 for arg in worker_args]
                
                # Monitor progress while workers are running
                last_update = 0
                while True:
                    # Check if all workers are done
                    if all(result.ready() for result in result_objects):
                        break
                    
                    # Update progress bar based on shared counter
                    current = progress_dict['completed']
                    if current > last_update:
                        pbar.update(current - last_update)
                        last_update = current
                        
                        # Update additional stats
                        if current > 0:
                            success_rate = 100 * progress_dict['successful'] / current
                            pbar.set_postfix({
                                "Completed": current, 
                                "Success %": f"{success_rate:.1f}%"
                            })
                    
                    # Don't hog CPU with constant checking
                    time.sleep(0.1)
                
                # Process results from all workers when complete
                for result_obj in result_objects:
                    worker_results = result_obj.get()
                    augmented_data.extend(worker_results)
        
        # Display summary statistics
        elapsed_time = time.time() - start_time
        seeds_per_second = len(augmented_data) / elapsed_time
        print(f"Augmentation completed in {elapsed_time:.1f} seconds ({seeds_per_second:.1f} seeds/second)")
        
        # Clean up temporary model file
        if os.path.exists(tmp_model_path):
            os.remove(tmp_model_path)
        
        # Save to cache
        augmented_data = np.array(augmented_data, dtype=object)
        print(f"Saving augmented data cache to {cache_file}")
        np.savez(cache_file, augmented_data=augmented_data)
        
        print(f"Added {len(augmented_data)} augmented samples")
        
        return augmented_data

# Add this function at the module level (outside any class)
def _augment_worker_process(args):
    """
    Worker function for parallel data augmentation with progress reporting.
    """
    start_seed, end_seed, tmp_model_path, base_ring, progress_dict = args
    
    # Create a new OrbitCorrector instance for this worker
    worker_corrector = OrbitCorrector(base_ring, device='cpu')
    
    # Load the model and scalers (with proper options)
    try:
        from sklearn.preprocessing import StandardScaler
        import torch.serialization
        torch.serialization.add_safe_globals([StandardScaler])
        checkpoint = torch.load(tmp_model_path, map_location='cpu')
    except:
        checkpoint = torch.load(tmp_model_path, map_location='cpu', weights_only=False)
    
    # Load model and scalers manually
    worker_corrector.model.load_state_dict(checkpoint['model_state_dict'])
    worker_corrector.trajectory_scaler = checkpoint['trajectory_scaler']
    worker_corrector.corrector_scaler = checkpoint['corrector_scaler']
    worker_corrector.initial_corrector_scaler = checkpoint['initial_corrector_scaler']
    
    worker_results = []
    
    # Process each seed in this worker's batch
    for seed_num in range(start_seed, end_seed + 1):
        try:
            # Load pre and post correction rings
            seed_file = f'./matlab/seeds/seed{seed_num:d}.mat'
            pre_ring = at.load_mat(seed_file, check=False, use="preCorrection")
            post_ring = at.load_mat(seed_file, check=False, use="postCorrection")
            
            # Get BPM readings and initial corrector values from pre-correction ring
            bpm_readings, _ = utils.getBPMreading(pre_ring)
            initial_hcm = utils.getCorrectorStrengths(pre_ring, 'x')
            initial_vcm = utils.getCorrectorStrengths(pre_ring, 'y')
            initial_correctors = np.concatenate([initial_hcm, initial_vcm])
            
            # Get target corrector values from post-correction ring (our label)
            target_hcm = utils.getCorrectorStrengths(post_ring, 'x')
            target_vcm = utils.getCorrectorStrengths(post_ring, 'y')
            target_corrections = np.concatenate([target_hcm, target_vcm])
            
            # Get model predictions for corrector settings
            predicted_corrections = worker_corrector.predict_corrections(bpm_readings, initial_correctors)
            
            # Apply predicted corrections to create intermediate state
            augmented_ring = pre_ring.copy()
            augmented_ring = utils.setCorrectorStrengths(augmented_ring, 'x', 
                                                       predicted_corrections[:len(worker_corrector.hcm)])
            augmented_ring = utils.setCorrectorStrengths(augmented_ring, 'y', 
                                                       predicted_corrections[len(worker_corrector.hcm):])
            
            # Get new BPM readings from intermediate state
            augmented_bpm_readings, _ = utils.getBPMreading(augmented_ring)
            
            # Store as new training sample
            worker_results.append([augmented_bpm_readings, predicted_corrections, target_corrections])
            
            # Update shared progress counter - count successful processing
            with progress_dict.get_lock():
                progress_dict['completed'] += 1
                progress_dict['successful'] += 1
            
        except Exception as e:
            print(f"Worker error processing seed {seed_num}: {str(e)}")
            # Update shared progress counter - count attempted processing
            with progress_dict.get_lock():
                progress_dict['completed'] += 1
            continue
            
    return worker_results

# Add this function at the module level (outside any class)
def _validate_worker_process(args):
    """
    Worker function for parallel validation with progress reporting.
    """
    seed_batch, tmp_model_path, base_ring, progress_dict = args
    
    # Create a new OrbitCorrector instance for this worker
    worker_corrector = OrbitCorrector(base_ring, device='cpu')
    
    # Load the model and scalers
    try:
        from sklearn.preprocessing import StandardScaler
        import torch.serialization
        torch.serialization.add_safe_globals([StandardScaler])
        checkpoint = torch.load(tmp_model_path, map_location='cpu')
    except:
        checkpoint = torch.load(tmp_model_path, map_location='cpu', weights_only=False)
    
    # Load model and scalers manually
    worker_corrector.model.load_state_dict(checkpoint['model_state_dict'])
    worker_corrector.trajectory_scaler = checkpoint['trajectory_scaler']
    worker_corrector.corrector_scaler = checkpoint['corrector_scaler']
    worker_corrector.initial_corrector_scaler = checkpoint['initial_corrector_scaler']
    
    worker_results = []
    
    # Process each seed in this worker's batch
    for seed_num in seed_batch:
        try:
            # Load pre and post correction rings
            lattice_file = f"./matlab/seeds/seed{seed_num:d}.mat"
            pre_ring = at.load_mat(lattice_file, check=False, use="preCorrection")
            post_ring = at.load_mat(lattice_file, check=False, use="postCorrection")

            # Get initial state
            [B0, T0] = utils.getBPMreading(pre_ring)
            initial_rms = utils.rms(np.concatenate(T0))
            initial_loss = utils.rms(B0)

            # Get current trajectory
            initial_hcm = utils.getCorrectorStrengths(pre_ring, 'x')
            initial_vcm = utils.getCorrectorStrengths(pre_ring, 'y')
            initial_correctors = np.concatenate([initial_hcm, initial_vcm])

            # Get model predictions for corrector settings
            predicted_corrections = worker_corrector.predict_corrections(B0, initial_correctors)

            # Apply predicted corrections
            pre_ring = utils.setCorrectorStrengths(pre_ring, 'x',
                                                predicted_corrections[:len(worker_corrector.hcm)])
            pre_ring = utils.setCorrectorStrengths(pre_ring, 'y',
                                                predicted_corrections[len(worker_corrector.hcm):])

            # Measure new state
            [B_new, T_new] = utils.getBPMreading(pre_ring)
            new_rms = utils.rms(np.concatenate(T_new))
            new_loss = utils.rms(B_new)

            # Get expected metrics
            target_hcm = utils.getCorrectorStrengths(post_ring, 'x')
            target_vcm = utils.getCorrectorStrengths(post_ring, 'y')
            pre_ring = utils.setCorrectorStrengths(pre_ring, 'x', target_hcm)
            pre_ring = utils.setCorrectorStrengths(pre_ring, 'y', target_vcm)
            [B1, T1] = utils.getBPMreading(pre_ring)
            expected_rms = utils.rms(np.concatenate(T1))
            expected_loss = utils.rms(B1)

            worker_results.append({
                'seed': seed_num,
                'rms_improvement': ((initial_rms - new_rms) / initial_rms) * 100,
                'expected rms_improvement': ((initial_rms - expected_rms) / initial_rms) * 100,
                'loss_improvement': ((initial_loss - new_loss) / initial_loss) * 100,
                'expected loss_improvement': ((initial_loss - expected_loss) / initial_loss) * 100,
            })
            
            # Update shared progress counter - count successful processing
            with progress_dict.get_lock():
                progress_dict['completed'] += 1
                progress_dict['successful'] += 1
                
        except Exception as e:
            print(f"Worker error processing seed {seed_num}: {str(e)}")
            # Update shared progress counter - count attempted processing
            with progress_dict.get_lock():
                progress_dict['completed'] += 1
                
    return worker_results
