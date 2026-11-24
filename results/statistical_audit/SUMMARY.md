# Statistical audit benchmark summary

Synthetic Monte Carlo results only; not observations from the hosted service.

Commit: `6a0ebe4e6f298a032116d927ee5149563192ffd5`
Trials: {'null': 1000, 'power': 400, 'alert_sequences': 200}
Runtime: 1236.8 s

## A. Null type-I error (BH-adjusted flags)

| correlation | method | detector | rate [95% CI] | k | n | ms/window |
|---|---|---|---|---|---|---|
| rho=0.00 | current | any | 0.9% [0.5, 1.7] | 9 | 1000 | 74.29 |
| rho=0.00 | paired | any | 4.5% [3.4, 6.0] | 45 | 1000 | 31.069 |
| rho=0.00 | current | dist | 0.2% [0.1, 0.7] | 2 | 1000 | 74.29 |
| rho=0.00 | paired | dist | 3.5% [2.5, 4.8] | 35 | 1000 | 31.069 |
| rho=0.00 | current | ks | 0.1% [0.0, 0.6] | 1 | 1000 | 74.29 |
| rho=0.00 | paired | ks | 5.4% [4.2, 7.0] | 54 | 1000 | 31.069 |
| rho=0.00 | current | mmd | 0.2% [0.1, 0.7] | 2 | 1000 | 74.29 |
| rho=0.00 | paired | mmd | 5.6% [4.3, 7.2] | 56 | 1000 | 31.069 |
| rho=0.00 | current | prop | 1.9% [1.2, 2.9] | 19 | 1000 | 74.29 |
| rho=0.00 | paired | prop | 2.4% [1.6, 3.5] | 24 | 1000 | 31.069 |
| rho=0.30 | current | any | 0.8% [0.4, 1.6] | 8 | 1000 | 74.262 |
| rho=0.30 | paired | any | 4.6% [3.5, 6.1] | 46 | 1000 | 31.236 |
| rho=0.30 | current | dist | 0.1% [0.0, 0.6] | 1 | 1000 | 74.262 |
| rho=0.30 | paired | dist | 3.7% [2.7, 5.1] | 37 | 1000 | 31.236 |
| rho=0.30 | current | ks | 0.1% [0.0, 0.6] | 1 | 1000 | 74.262 |
| rho=0.30 | paired | ks | 4.9% [3.7, 6.4] | 49 | 1000 | 31.236 |
| rho=0.30 | current | mmd | 0.5% [0.2, 1.2] | 5 | 1000 | 74.262 |
| rho=0.30 | paired | mmd | 6.9% [5.5, 8.6] | 69 | 1000 | 31.236 |
| rho=0.30 | current | prop | 1.9% [1.2, 2.9] | 19 | 1000 | 74.262 |
| rho=0.30 | paired | prop | 2.4% [1.6, 3.5] | 24 | 1000 | 31.236 |
| rho=0.60 | current | any | 0.9% [0.5, 1.7] | 9 | 1000 | 73.117 |
| rho=0.60 | paired | any | 5.0% [3.8, 6.5] | 50 | 1000 | 30.688 |
| rho=0.60 | current | dist | 0.2% [0.1, 0.7] | 2 | 1000 | 73.117 |
| rho=0.60 | paired | dist | 4.0% [3.0, 5.4] | 40 | 1000 | 30.688 |
| rho=0.60 | current | ks | 0.2% [0.1, 0.7] | 2 | 1000 | 73.117 |
| rho=0.60 | paired | ks | 4.5% [3.4, 6.0] | 45 | 1000 | 30.688 |
| rho=0.60 | current | mmd | 1.3% [0.8, 2.2] | 13 | 1000 | 73.117 |
| rho=0.60 | paired | mmd | 6.3% [5.0, 8.0] | 63 | 1000 | 30.688 |
| rho=0.60 | current | prop | 1.9% [1.2, 2.9] | 19 | 1000 | 73.117 |
| rho=0.60 | paired | prop | 2.4% [1.6, 3.5] | 24 | 1000 | 30.688 |

## B. Power by shift condition

| condition | magnitude | detector | method | rate [95% CI] | k | n | ms/window |
|---|---|---|---|---|---|---|---|
| B_length | 0.3 | ks | current | 7.8% [5.5, 10.8] | 31 | 400 | 74.069 |
| B_length | 0.3 | ks | paired | 82.5% [78.5, 85.9] | 330 | 400 | 31.139 |
| B_length | 0.3 | prop | current | 3.0% [1.7, 5.2] | 12 | 400 | 74.069 |
| B_length | 0.3 | prop | paired | 3.5% [2.1, 5.8] | 14 | 400 | 31.139 |
| B_length | 0.3 | mmd | current | 0.0% [0.0, 1.0] | 0 | 400 | 74.069 |
| B_length | 0.3 | mmd | paired | 5.0% [3.3, 7.6] | 20 | 400 | 31.139 |
| B_length | 0.3 | any | current | 3.0% [1.7, 5.2] | 12 | 400 | 74.069 |
| B_length | 0.3 | any | paired | 68.0% [63.3, 72.4] | 272 | 400 | 31.139 |
| B_length | 0.3 | dist | current | 1.8% [0.9, 3.6] | 7 | 400 | 74.069 |
| B_length | 0.3 | dist | paired | 67.5% [62.8, 71.9] | 270 | 400 | 31.139 |
| B_length | 0.6 | ks | current | 67.5% [62.8, 71.9] | 270 | 400 | 70.946 |
| B_length | 0.6 | ks | paired | 100.0% [99.0, 100.0] | 400 | 400 | 29.881 |
| B_length | 0.6 | prop | current | 3.0% [1.7, 5.2] | 12 | 400 | 70.946 |
| B_length | 0.6 | prop | paired | 3.5% [2.1, 5.8] | 14 | 400 | 29.881 |
| B_length | 0.6 | mmd | current | 0.0% [0.0, 1.0] | 0 | 400 | 70.946 |
| B_length | 0.6 | mmd | paired | 5.0% [3.3, 7.6] | 20 | 400 | 29.881 |
| B_length | 0.6 | any | current | 43.2% [38.5, 48.1] | 173 | 400 | 70.946 |
| B_length | 0.6 | any | paired | 99.8% [98.6, 100.0] | 399 | 400 | 29.881 |
| B_length | 0.6 | dist | current | 42.5% [37.7, 47.4] | 170 | 400 | 70.946 |
| B_length | 0.6 | dist | paired | 99.8% [98.6, 100.0] | 399 | 400 | 29.881 |
| B_length | 1.2 | ks | current | 100.0% [99.0, 100.0] | 400 | 400 | 77.841 |
| B_length | 1.2 | ks | paired | 100.0% [99.0, 100.0] | 400 | 400 | 32.791 |
| B_length | 1.2 | prop | current | 3.0% [1.7, 5.2] | 12 | 400 | 77.841 |
| B_length | 1.2 | prop | paired | 3.5% [2.1, 5.8] | 14 | 400 | 32.791 |
| B_length | 1.2 | mmd | current | 0.0% [0.0, 1.0] | 0 | 400 | 77.841 |
| B_length | 1.2 | mmd | paired | 5.0% [3.3, 7.6] | 20 | 400 | 32.791 |
| B_length | 1.2 | any | current | 100.0% [99.0, 100.0] | 400 | 400 | 77.841 |
| B_length | 1.2 | any | paired | 100.0% [99.0, 100.0] | 400 | 400 | 32.791 |
| B_length | 1.2 | dist | current | 100.0% [99.0, 100.0] | 400 | 400 | 77.841 |
| B_length | 1.2 | dist | paired | 100.0% [99.0, 100.0] | 400 | 400 | 32.791 |
| C_semantic_mean | 0.25 | ks | current | 0.0% [0.0, 1.0] | 0 | 400 | 80.454 |
| C_semantic_mean | 0.25 | ks | paired | 4.5% [2.9, 7.0] | 18 | 400 | 33.871 |
| C_semantic_mean | 0.25 | prop | current | 3.0% [1.7, 5.2] | 12 | 400 | 80.454 |
| C_semantic_mean | 0.25 | prop | paired | 3.5% [2.1, 5.8] | 14 | 400 | 33.871 |
| C_semantic_mean | 0.25 | mmd | current | 89.0% [85.6, 91.7] | 356 | 400 | 80.454 |
| C_semantic_mean | 0.25 | mmd | paired | 99.2% [97.8, 99.7] | 397 | 400 | 33.871 |
| C_semantic_mean | 0.25 | any | current | 79.2% [75.0, 82.9] | 317 | 400 | 80.454 |
| C_semantic_mean | 0.25 | any | paired | 97.8% [95.8, 98.8] | 391 | 400 | 33.871 |
| C_semantic_mean | 0.25 | dist | current | 79.0% [74.7, 82.7] | 316 | 400 | 80.454 |
| C_semantic_mean | 0.25 | dist | paired | 97.8% [95.8, 98.8] | 391 | 400 | 33.871 |
| C_semantic_mean | 0.5 | ks | current | 0.0% [0.0, 1.0] | 0 | 400 | 71.805 |
| C_semantic_mean | 0.5 | ks | paired | 4.5% [2.9, 7.0] | 18 | 400 | 30.514 |
| C_semantic_mean | 0.5 | prop | current | 3.0% [1.7, 5.2] | 12 | 400 | 71.805 |
| C_semantic_mean | 0.5 | prop | paired | 3.5% [2.1, 5.8] | 14 | 400 | 30.514 |
| C_semantic_mean | 0.5 | mmd | current | 100.0% [99.0, 100.0] | 400 | 400 | 71.805 |
| C_semantic_mean | 0.5 | mmd | paired | 100.0% [99.0, 100.0] | 400 | 400 | 30.514 |
| C_semantic_mean | 0.5 | any | current | 100.0% [99.0, 100.0] | 400 | 400 | 71.805 |
| C_semantic_mean | 0.5 | any | paired | 100.0% [99.0, 100.0] | 400 | 400 | 30.514 |
| C_semantic_mean | 0.5 | dist | current | 100.0% [99.0, 100.0] | 400 | 400 | 71.805 |
| C_semantic_mean | 0.5 | dist | paired | 100.0% [99.0, 100.0] | 400 | 400 | 30.514 |
| C_semantic_mean | 1.0 | ks | current | 0.0% [0.0, 1.0] | 0 | 400 | 71.545 |
| C_semantic_mean | 1.0 | ks | paired | 4.5% [2.9, 7.0] | 18 | 400 | 30.466 |
| C_semantic_mean | 1.0 | prop | current | 3.0% [1.7, 5.2] | 12 | 400 | 71.545 |
| C_semantic_mean | 1.0 | prop | paired | 3.5% [2.1, 5.8] | 14 | 400 | 30.466 |
| C_semantic_mean | 1.0 | mmd | current | 100.0% [99.0, 100.0] | 400 | 400 | 71.545 |
| C_semantic_mean | 1.0 | mmd | paired | 100.0% [99.0, 100.0] | 400 | 400 | 30.466 |
| C_semantic_mean | 1.0 | any | current | 100.0% [99.0, 100.0] | 400 | 400 | 71.545 |
| C_semantic_mean | 1.0 | any | paired | 100.0% [99.0, 100.0] | 400 | 400 | 30.466 |
| C_semantic_mean | 1.0 | dist | current | 100.0% [99.0, 100.0] | 400 | 400 | 71.545 |
| C_semantic_mean | 1.0 | dist | paired | 100.0% [99.0, 100.0] | 400 | 400 | 30.466 |
| D_semantic_shape | 1.2 | ks | current | 0.0% [0.0, 1.0] | 0 | 400 | 71.921 |
| D_semantic_shape | 1.2 | ks | paired | 4.5% [2.9, 7.0] | 18 | 400 | 30.573 |
| D_semantic_shape | 1.2 | prop | current | 3.0% [1.7, 5.2] | 12 | 400 | 71.921 |
| D_semantic_shape | 1.2 | prop | paired | 3.5% [2.1, 5.8] | 14 | 400 | 30.573 |
| D_semantic_shape | 1.2 | mmd | current | 0.0% [0.0, 1.0] | 0 | 400 | 71.921 |
| D_semantic_shape | 1.2 | mmd | paired | 7.0% [4.9, 9.9] | 28 | 400 | 30.573 |
| D_semantic_shape | 1.2 | any | current | 1.2% [0.5, 2.9] | 5 | 400 | 71.921 |
| D_semantic_shape | 1.2 | any | paired | 7.0% [4.9, 9.9] | 28 | 400 | 30.573 |
| D_semantic_shape | 1.2 | dist | current | 0.0% [0.0, 1.0] | 0 | 400 | 71.921 |
| D_semantic_shape | 1.2 | dist | paired | 5.8% [3.9, 8.5] | 23 | 400 | 30.573 |
| D_semantic_shape | 1.5 | ks | current | 0.0% [0.0, 1.0] | 0 | 400 | 70.901 |
| D_semantic_shape | 1.5 | ks | paired | 4.5% [2.9, 7.0] | 18 | 400 | 30.075 |
| D_semantic_shape | 1.5 | prop | current | 3.0% [1.7, 5.2] | 12 | 400 | 70.901 |
| D_semantic_shape | 1.5 | prop | paired | 3.5% [2.1, 5.8] | 14 | 400 | 30.075 |
| D_semantic_shape | 1.5 | mmd | current | 0.8% [0.3, 2.2] | 3 | 400 | 70.901 |
| D_semantic_shape | 1.5 | mmd | paired | 11.2% [8.5, 14.7] | 45 | 400 | 30.075 |
| D_semantic_shape | 1.5 | any | current | 1.2% [0.5, 2.9] | 5 | 400 | 70.901 |
| D_semantic_shape | 1.5 | any | paired | 9.2% [6.8, 12.5] | 37 | 400 | 30.075 |
| D_semantic_shape | 1.5 | dist | current | 0.0% [0.0, 1.0] | 0 | 400 | 70.901 |
| D_semantic_shape | 1.5 | dist | paired | 8.0% [5.7, 11.1] | 32 | 400 | 30.075 |
| D_semantic_shape | 2.0 | ks | current | 0.0% [0.0, 1.0] | 0 | 400 | 70.51 |
| D_semantic_shape | 2.0 | ks | paired | 4.5% [2.9, 7.0] | 18 | 400 | 30.0 |
| D_semantic_shape | 2.0 | prop | current | 3.0% [1.7, 5.2] | 12 | 400 | 70.51 |
| D_semantic_shape | 2.0 | prop | paired | 3.5% [2.1, 5.8] | 14 | 400 | 30.0 |
| D_semantic_shape | 2.0 | mmd | current | 1.0% [0.4, 2.5] | 4 | 400 | 70.51 |
| D_semantic_shape | 2.0 | mmd | paired | 18.2% [14.8, 22.3] | 73 | 400 | 30.0 |
| D_semantic_shape | 2.0 | any | current | 1.2% [0.5, 2.9] | 5 | 400 | 70.51 |
| D_semantic_shape | 2.0 | any | paired | 11.5% [8.7, 15.0] | 46 | 400 | 30.0 |
| D_semantic_shape | 2.0 | dist | current | 0.0% [0.0, 1.0] | 0 | 400 | 70.51 |
| D_semantic_shape | 2.0 | dist | paired | 10.2% [7.6, 13.6] | 41 | 400 | 30.0 |
| E_availability | 0.05 | ks | current | 0.0% [0.0, 1.0] | 0 | 400 | 68.498 |
| E_availability | 0.05 | ks | paired | 4.0% [2.5, 6.4] | 16 | 400 | 29.757 |
| E_availability | 0.05 | prop | current | 14.5% [11.4, 18.3] | 58 | 400 | 68.498 |
| E_availability | 0.05 | prop | paired | 16.0% [12.7, 19.9] | 64 | 400 | 29.757 |
| E_availability | 0.05 | mmd | current | 0.0% [0.0, 1.0] | 0 | 400 | 68.498 |
| E_availability | 0.05 | mmd | paired | 5.2% [3.5, 7.9] | 21 | 400 | 29.757 |
| E_availability | 0.05 | any | current | 6.8% [4.7, 9.6] | 27 | 400 | 68.498 |
| E_availability | 0.05 | any | paired | 13.2% [10.3, 16.9] | 53 | 400 | 29.757 |
| E_availability | 0.05 | dist | current | 0.0% [0.0, 1.0] | 0 | 400 | 68.498 |
| E_availability | 0.05 | dist | paired | 3.5% [2.1, 5.8] | 14 | 400 | 29.757 |
| E_availability | 0.15 | ks | current | 0.0% [0.0, 1.0] | 0 | 400 | 58.994 |
| E_availability | 0.15 | ks | paired | 4.8% [3.1, 7.3] | 19 | 400 | 27.091 |
| E_availability | 0.15 | prop | current | 72.8% [68.2, 76.9] | 291 | 400 | 58.994 |
| E_availability | 0.15 | prop | paired | 76.5% [72.1, 80.4] | 306 | 400 | 27.091 |
| E_availability | 0.15 | mmd | current | 0.0% [0.0, 1.0] | 0 | 400 | 58.994 |
| E_availability | 0.15 | mmd | paired | 5.0% [3.3, 7.6] | 20 | 400 | 27.091 |
| E_availability | 0.15 | any | current | 54.5% [49.6, 59.3] | 218 | 400 | 58.994 |
| E_availability | 0.15 | any | paired | 63.5% [58.7, 68.1] | 254 | 400 | 27.091 |
| E_availability | 0.15 | dist | current | 0.0% [0.0, 1.0] | 0 | 400 | 58.994 |
| E_availability | 0.15 | dist | paired | 6.0% [4.1, 8.8] | 24 | 400 | 27.091 |
| E_availability | 0.25 | ks | current | 0.5% [0.1, 1.8] | 2 | 400 | 51.311 |
| E_availability | 0.25 | ks | paired | 4.2% [2.7, 6.7] | 17 | 400 | 24.588 |
| E_availability | 0.25 | prop | current | 96.8% [94.5, 98.1] | 387 | 400 | 51.311 |
| E_availability | 0.25 | prop | paired | 97.0% [94.8, 98.3] | 388 | 400 | 24.588 |
| E_availability | 0.25 | mmd | current | 0.0% [0.0, 1.0] | 0 | 400 | 51.311 |
| E_availability | 0.25 | mmd | paired | 5.0% [3.3, 7.6] | 20 | 400 | 24.588 |
| E_availability | 0.25 | any | current | 92.8% [89.8, 94.9] | 371 | 400 | 51.311 |
| E_availability | 0.25 | any | paired | 93.8% [90.9, 95.7] | 375 | 400 | 24.588 |
| E_availability | 0.25 | dist | current | 0.2% [0.0, 1.4] | 1 | 400 | 51.311 |
| E_availability | 0.25 | dist | paired | 5.8% [3.9, 8.5] | 23 | 400 | 24.588 |
| F_combined | 0.5 | ks | current | 60.5% [55.6, 65.2] | 242 | 400 | 60.488 |
| F_combined | 0.5 | ks | paired | 100.0% [99.0, 100.0] | 400 | 400 | 28.123 |
| F_combined | 0.5 | prop | current | 72.8% [68.2, 76.9] | 291 | 400 | 60.488 |
| F_combined | 0.5 | prop | paired | 76.5% [72.1, 80.4] | 306 | 400 | 28.123 |
| F_combined | 0.5 | mmd | current | 99.8% [98.6, 100.0] | 399 | 400 | 60.488 |
| F_combined | 0.5 | mmd | paired | 100.0% [99.0, 100.0] | 400 | 400 | 28.123 |
| F_combined | 0.5 | any | current | 100.0% [99.0, 100.0] | 400 | 400 | 60.488 |
| F_combined | 0.5 | any | paired | 100.0% [99.0, 100.0] | 400 | 400 | 28.123 |
| F_combined | 0.5 | dist | current | 99.8% [98.6, 100.0] | 399 | 400 | 60.488 |
| F_combined | 0.5 | dist | paired | 100.0% [99.0, 100.0] | 400 | 400 | 28.123 |
| F_combined | 1.0 | ks | current | 100.0% [99.0, 100.0] | 400 | 400 | 53.216 |
| F_combined | 1.0 | ks | paired | 100.0% [99.0, 100.0] | 400 | 400 | 25.723 |
| F_combined | 1.0 | prop | current | 96.8% [94.5, 98.1] | 387 | 400 | 53.216 |
| F_combined | 1.0 | prop | paired | 97.0% [94.8, 98.3] | 388 | 400 | 25.723 |
| F_combined | 1.0 | mmd | current | 100.0% [99.0, 100.0] | 400 | 400 | 53.216 |
| F_combined | 1.0 | mmd | paired | 100.0% [99.0, 100.0] | 400 | 400 | 25.723 |
| F_combined | 1.0 | any | current | 100.0% [99.0, 100.0] | 400 | 400 | 53.216 |
| F_combined | 1.0 | any | paired | 100.0% [99.0, 100.0] | 400 | 400 | 25.723 |
| F_combined | 1.0 | dist | current | 100.0% [99.0, 100.0] | 400 | 400 | 53.216 |
| F_combined | 1.0 | dist | paired | 100.0% [99.0, 100.0] | 400 | 400 | 25.723 |

## C. Two-cycle alert policy

| condition | correlation | method | rate [95% CI] | k | n |
|---|---|---|---|---|---|
| alert_null | rho=0.00 | current | 0.0% [0.0, 1.9] | 0 | 200 |
| alert_null | rho=0.00 | paired | 1.5% [0.5, 4.3] | 3 | 200 |
| alert_shift | rho=0.00 | current | 81.5% [75.5, 86.3] | 163 | 200 |
| alert_shift | rho=0.00 | paired | 100.0% [98.1, 100.0] | 200 | 200 |
| alert_null | rho=0.30 | current | 0.0% [0.0, 1.9] | 0 | 200 |
| alert_null | rho=0.30 | paired | 0.5% [0.1, 2.8] | 1 | 200 |
| alert_shift | rho=0.30 | current | 77.0% [70.7, 82.3] | 154 | 200 |
| alert_shift | rho=0.30 | paired | 99.0% [96.4, 99.7] | 198 | 200 |

## D. Detection delay under combined shift

| correlation | method | detected | sequences | rate [95% CI] | mean delay (cycles) |
|---|---|---|---|---|---|
| 0.0 | current | 163 | 200 | 81.5% [75.5, 86.3] | 2.01840490797546 |
| 0.0 | paired | 200 | 200 | 100.0% [98.1, 100.0] | 2.0 |
| 0.3 | current | 154 | 200 | 77.0% [70.7, 82.3] | 2.0194805194805197 |
| 0.3 | paired | 198 | 200 | 99.0% [96.4, 99.7] | 2.0 |
