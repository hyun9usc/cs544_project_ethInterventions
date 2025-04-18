1. Clone the ethical interventions GitHub
2. Copy files in this folder into ethical interventions clone and replace the conflicting files with these ones.
3. pip install all the files in requirements.txt
	- If any conflicts occur when trying to run due to absent packages, download those too (in case I forgor a package :P)
4. Download the SQUAD train-v1.1.json (https://github.com/rajpurkar/SQuAD-explorer/blob/master/dataset/train-v1.1.json)
	- Make sure it is placed into folder: /ethical_interventions_base/inputs/train-v1.1.json
4. In ./run.sh, edit line 16 `for LR in 5e-6 1e-5 2e-5 3e-5` and line 18 `for NUM_EPOCH in 3 5 7 9` if you don't want to run all the learning rates and all the epoch nums.
5. Do: ./run.sh data/underspecified outputs 64 4 4 1 "--doirrelevant --squad" U_ US theta4 1 0.5 1
	- Run.sh currently only 